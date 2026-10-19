// Parity.app -- a native macOS shell around the local Parity server.
//
// On launch it starts the Python server (the same code the CLI runs) as a child
// process, waits for it to answer, and shows the UI in a WKWebView window. The
// app owns the server: quitting it stops the server, unless the server was
// already running when the app opened (in which case it is left alone).
//
// Built by tools/build_macos_app.py. Command line arguments (mainly for testing
// and power users): --port N, --data-dir PATH, --no-server.

import Cocoa
import WebKit

// MARK: - Small helpers

func nowStamp() -> String {
    let formatter = DateFormatter()
    formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return formatter.string(from: Date())
}

final class Log {
    let url: URL
    private let lock = NSLock()

    init(url: URL) {
        self.url = url
    }

    func write(_ message: String) {
        let line = "[\(nowStamp())] \(message)\n"
        FileHandle.standardOutput.write(line.data(using: .utf8)!)
        lock.lock()
        defer { lock.unlock() }
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(line.data(using: .utf8)!)
            try? handle.close()
        } else {
            try? line.data(using: .utf8)?.write(to: url)
        }
    }
}

func freePort(_ port: Int) -> Bool {
    let descriptor = socket(AF_INET, SOCK_STREAM, 0)
    guard descriptor >= 0 else { return false }
    defer { close(descriptor) }
    var address = sockaddr_in()
    address.sin_family = sa_family_t(AF_INET)
    address.sin_port = in_port_t(UInt16(port).bigEndian)
    address.sin_addr.s_addr = inet_addr("127.0.0.1")
    let result = withUnsafePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return result == 0
}

// MARK: - Application

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var serverProcess: Process?
    private var startedServer = false
    private var port = 8765
    private var selftest = false
    private var selftestStarted = false

    private let supportDir = FileManager.default
        .homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Parity", isDirectory: true)

    private var log: Log!
    private var serverLog: URL { supportDir.appendingPathComponent("parity.log") }
    private var appLog: URL { supportDir.appendingPathComponent("app.log") }
    private var pidFile: URL { supportDir.appendingPathComponent("server.pid") }

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        try? FileManager.default.createDirectory(at: supportDir, withIntermediateDirectories: true)
        log = Log(url: appLog)
        selftest = CommandLine.arguments.contains("--selftest")
        log.write("Parity launching (bundle: \(Bundle.main.bundlePath))")

        buildMenu()
        cleanUpOrphanedServers()
        configurePort()
        startServer()

        showWindow()

        if serverIsUp() {
            loadUI()
        } else {
            fail("Parity could not start its local server.",
                 detail: "The Python server did not answer on port \(port).\n\nLog: \(serverLog.path)")
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        stopOwnedServer()
    }

    /// The app always runs its own server, so quitting always stops one (the
    /// server you may have started from the terminal is a separate thing and is
    /// never touched).
    private func stopOwnedServer() {
        guard startedServer, let process = serverProcess, process.isRunning else { return }
        log.write("stopping the server (pid \(process.processIdentifier))")
        process.terminate()
        process.waitUntilExit()
        forgetServer(pid: process.processIdentifier)
    }

    /// The servers this app started on earlier runs, as "pid port" lines.
    ///
    /// One line per instance rather than a single record: a second copy of the
    /// app (or a self-test run) must not overwrite the record that lets a later
    /// launch tidy up after *this* one, and each instance owns its own line.
    private func rememberedServers() -> [(pid: Int32, port: Int)] {
        guard let text = try? String(contentsOf: pidFile, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap { line in
            let parts = line.split(separator: " ")
            guard parts.count == 2, let pid = Int32(parts[0]), let port = Int(parts[1]) else { return nil }
            return (pid, port)
        }
    }

    private func rememberServers(_ servers: [(pid: Int32, port: Int)]) {
        guard !servers.isEmpty else {
            try? FileManager.default.removeItem(at: pidFile)
            return
        }
        let text = servers.map { "\($0.pid) \($0.port)" }.joined(separator: "\n") + "\n"
        try? text.write(to: pidFile, atomically: true, encoding: .utf8)
    }

    /// Records this instance's server, leaving other instances' lines in place.
    private func rememberServer(pid: Int32, port: Int) {
        rememberServers(rememberedServers().filter { $0.pid != pid } + [(pid, port)])
    }

    /// Drops this instance's line, keeping any other instance's record.
    private func forgetServer(pid: Int32) {
        rememberServers(rememberedServers().filter { $0.pid != pid })
    }

    /// The parent of ``pid``, or nil when it cannot be read.
    ///
    /// A server started by an app has that app as its parent; one whose app is
    /// gone has been reparented to launchd (pid 1). That difference is what
    /// tells a leftover server from one a window is still using.
    private func parentProcess(of pid: Int32) -> Int32? {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        let written = withUnsafeMutablePointer(to: &info) { pointer in
            proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, pointer, size)
        }
        guard written == size else { return nil }
        return Int32(info.pbi_ppid)
    }

    /// A server a previous run started and then crashed on is ours to stop. A
    /// server whose parent is still alive is not.
    ///
    /// That one belongs to a Parity window that is open right now -- a second
    /// copy of the app, or a self-test run -- and stopping it blanks the window
    /// the user is looking at, which cannot recover on its own. It is left alone
    /// with its record intact, and this instance takes a port of its own.
    private func cleanUpOrphanedServers() {
        var survivors: [(pid: Int32, port: Int)] = []
        for record in rememberedServers() {
            guard kill(record.pid, 0) == 0 else { continue }      // already gone
            guard serverIsUp(on: record.port) else { continue }   // not ours to touch

            if let owner = parentProcess(of: record.pid), owner > 1 {
                log.write("a running instance still owns the server on port \(record.port) "
                          + "(pid \(record.pid), parent \(owner)); leaving it alone")
                survivors.append(record)
                continue
            }

            log.write("cleaning up an orphaned server from a previous run (pid \(record.pid))")
            kill(record.pid, SIGTERM)
            for _ in 0..<20 where kill(record.pid, 0) == 0 {
                Thread.sleep(forTimeInterval: 0.1)
            }
        }
        rememberServers(survivors)
    }

    // MARK: arguments

    /// Picks a free port, preferring 8765.
    ///
    /// The app deliberately never adopts a server it did not start: one from the
    /// CLI could be running older code, or a different data folder, and then
    /// imports and exports quietly disagree with what is on screen.
    private func configurePort() {
        let arguments = CommandLine.arguments
        if let index = arguments.firstIndex(of: "--port"), index + 1 < arguments.count,
           let requested = Int(arguments[index + 1]) {
            if freePort(requested) {
                port = requested
                return
            }
            log.write("requested port \(requested) is busy, picking another")
        }

        if freePort(8765) {
            port = 8765
            return
        }
        for candidate in 8766...8790 where freePort(candidate) {
            port = candidate
            log.write("port 8765 is busy, using \(candidate)")
            return
        }
    }

    private func dataDirectory() -> String {
        let arguments = CommandLine.arguments
        if let index = arguments.firstIndex(of: "--data-dir"), index + 1 < arguments.count {
            return arguments[index + 1]
        }
        return supportDir.path
    }

    // MARK: server

    private func serverIsUp(on requestedPort: Int? = nil) -> Bool {
        let target = requestedPort ?? port
        let url = URL(string: "http://127.0.0.1:\(target)/api/state")!
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 2
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        let session = URLSession(configuration: configuration)
        var healthy = false
        let semaphore = DispatchSemaphore(value: 0)
        session.dataTask(with: url) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode == 200 { healthy = true }
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 3)
        session.invalidateAndCancel()
        return healthy
    }

    private func pythonBinary() -> String? {
        let candidates = (Bundle.main.object(forInfoDictionaryKey: "ParityPythonCandidates") as? [String]) ?? []
        for candidate in candidates where FileManager.default.isExecutableFile(atPath: candidate) {
            return candidate
        }
        // Last resort: whatever PATH gives us.
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", "-c", "import sys; print(sys.executable)"]
        let pipe = Pipe()
        process.standardOutput = pipe
        do {
            try process.run()
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            if let path, !path.isEmpty { return path }
        } catch {
            return nil
        }
        return nil
    }

    private func startServer() {
        guard let python = pythonBinary() else {
            fail("Parity needs Python 3.",
                 detail: "No Python 3 interpreter was found. Install it from python.org, then reopen Parity.")
            return
        }
        let payload = Bundle.main.resourceURL!.appendingPathComponent("Parity/parity.py")
        guard FileManager.default.fileExists(atPath: payload.path) else {
            fail("Parity's server code is missing from the app bundle.",
                 detail: "Rebuild the app with tools/build_macos_app.py.")
            return
        }

        FileManager.default.createFile(atPath: serverLog.path, contents: nil)
        let handle = FileHandle(forWritingAtPath: serverLog.path)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [
            payload.path, "--no-browser",
            "--port", String(port),
            "--data-dir", dataDirectory(),
        ]
        if let handle { process.standardOutput = handle; process.standardError = handle }

        do {
            try process.run()
        } catch {
            fail("Parity could not start its server.", detail: "\(error)")
            return
        }
        serverProcess = process
        startedServer = true
        log.write("started \(python) on port \(port) (pid \(process.processIdentifier))")
        // Remembered so a crash can be cleaned up on the next launch.
        rememberServer(pid: process.processIdentifier, port: port)

        // Wait for it to answer before showing the page.
        for _ in 0..<80 {
            if serverIsUp() { return }
            if !process.isRunning { break }
            Thread.sleep(forTimeInterval: 0.25)
        }
    }

    // MARK: window

    private func showWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1320, height: 860), configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1320, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Parity"
        window.titlebarAppearsTransparent = true
        window.minSize = NSSize(width: 900, height: 600)
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("ParityWindow")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func loadUI() {
        // shell=app tells the page it is inside the app window, so exports are
        // written to disk by the server instead of using a download the
        // web view would silently ignore.
        let address = URL(string: "http://127.0.0.1:\(port)/?shell=app")!
        log.write("loading \(address.absoluteString)")
        webView.load(URLRequest(url: address))
    }

    private func fail(_ message: String, detail: String) {
        log.write("error: \(message) \(detail)")
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = detail
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Quit Parity")
        alert.runModal()
        NSApp.terminate(nil)
    }

    // MARK: navigation

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        webView.evaluateJavaScript("document.title") { value, _ in
            let title = (value as? String) ?? "(no title)"
            self.log.write("page loaded: \(title)")
        }
        if selftest { runSelfTest(webView) }
    }

    // MARK: choosing a folder

    /// The page is never told the path of the folder the user picked -- a web
    /// page cannot look at the disk, and a directory <input> reports relative
    /// names and a fake root -- so the app answers the picker for it.  The
    /// "Choose…" buttons in the rename & convert panel click a hidden
    /// <input type="file" webkitdirectory>, which brings WebKit here instead of
    /// to a panel of its own: we put up a native choose-a-folder sheet and hand
    /// the path back through parityPickedPath (see pickFolder in app.js).
    func webView(_ webView: WKWebView,
                 runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        // Answering with nothing keeps WebKit's own panel closed; the path
        // travels back through JavaScript rather than through files.
        completionHandler([])
        log.write("folder picker: requested")

        // The page names the field that asked, and where it already points, as
        // it clicks -- see pickFolder in app.js.
        let probe = "[window.__parityPickTarget || '', window.__parityPickStart || '']"
        webView.evaluateJavaScript(probe) { value, error in
            if let error {
                self.log.write("folder picker: could not read the request: \(error.localizedDescription)")
            }
            let wanted = (value as? [String]) ?? []
            let target = wanted.first ?? ""
            guard !target.isEmpty else {
                self.log.write("folder picker: the page did not say which field asked, so nothing was shown")
                return
            }
            self.chooseFolder(for: target, startingAt: wanted.count > 1 ? wanted[1] : "", in: webView)
        }
    }

    private func chooseFolder(for target: String, startingAt start: String, in webView: WKWebView) {
        if selftest {
            // Never block an automated run on a modal sheet.  The answer names
            // the field it is for, so what gets checked is the bridge: the page
            // filling that field from a real path.
            log.write("folder picker: selftest, answering for \(target)")
            deliver(path: "/tmp/parity-pick-\(target)", to: target, in: webView)
            return
        }

        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = true
        panel.prompt = "Choose"
        panel.message = target == "destination"
            ? "Choose where the converted files should be written."
            : target == "watch"
                ? "Choose the folder new tracks land in."
                : "Choose the folder your downloads are in."
        // Open where the field already points, if it points anywhere real.
        var isDirectory: ObjCBool = false
        if !start.isEmpty,
           FileManager.default.fileExists(atPath: start, isDirectory: &isDirectory),
           isDirectory.boolValue {
            panel.directoryURL = URL(fileURLWithPath: start)
        }

        let respond: (NSApplication.ModalResponse) -> Void = { response in
            guard response == .OK, let url = panel.url else {
                self.log.write("folder picker: cancelled for \(target)")
                return
            }
            self.deliver(path: url.path, to: target, in: webView)
        }
        if let window {
            panel.beginSheetModal(for: window, completionHandler: respond)
        } else {
            panel.begin(completionHandler: respond)
        }
    }

    private func deliver(path: String, to target: String, in webView: WKWebView) {
        log.write("folder picker: \(target) = \(path)")
        webView.evaluateJavaScript("window.parityPickedPath(\(jsString(target)), \(jsString(path)))") { _, error in
            if let error {
                self.log.write("folder picker: the page refused the path: \(error.localizedDescription)")
            }
        }
    }

    /// A JavaScript string literal: escaped as JSON, so a path holding a quote
    /// or a backslash cannot break the call it is pasted into.
    private func jsString(_ value: String) -> String {
        let fallback = #"[""]"#
        guard let data = try? JSONSerialization.data(withJSONObject: [value]),
              let array = String(data: data, encoding: .utf8) else { return fallback }
        return String(array.dropFirst().dropLast())
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        log.write("navigation failed: \(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        log.write("could not reach the server: \(error.localizedDescription)")
    }

    // Open external links in the browser instead of inside the app window.
    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, let host = url.host,
           host != "127.0.0.1" && host != "localhost" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // MARK: self test

    /// Checks the app's own plumbing, not the UI: run the binary with
    /// ``--selftest`` and read the log and exit code.
    ///
    /// The folder picker is driven from here rather than clicked by hand: the
    /// page asks, the app answers with a path named after the field, and the page
    /// fills that field in.  Both ends of the bridge are checked, and a failed
    /// step exits non-zero.
    private func runSelfTest(_ webView: WKWebView) {
        guard !selftestStarted else { return }
        selftestStarted = true
        log.write("selftest: starting")

        // label, JavaScript, and how long to wait afterwards for the app to have
        // answered -- the picker arrives back from an app-side evaluateJavaScript.
        let steps: [(String, String, TimeInterval)] = [
            // The Import button is gone -- the library comes from rekordbox's own
            // database -- so this probes what replaced it: the drop-in fallback,
            // a top-level function in app.js.  Reaching it also proves the UI
            // script ran and parsed, which is the part worth knowing here.
            ("shell", "new URLSearchParams(location.search).get('shell')", 0),
            ("xml drop-in fallback", "String(typeof importXml === 'function')", 0.2),
            ("picker asked for the source", "pickFolder('source'); true", 0.6),
            ("source field", "document.querySelector('#ingestSource').value", 0.2),
            ("picker asked for the destination", "pickFolder('destination'); true", 0.6),
            ("destination field", "document.querySelector('#ingestDestination').value", 0),
            // The cue editor draws on a canvas, which is the one part of the UI
            // that a Chrome run cannot vouch for: this engine is WebKit. Opening
            // it on a real track reads the file and draws it, so a canvas call
            // this engine does not have would show up here rather than in the
            // user's lap.
            ("cue editor present",
             "String(typeof openCueEditor === 'function' && !!document.getElementById('cueCanvas'))",
             0.2),
            ("cue editor opened",
             """
             (() => {
               const track = state.pool.find((item) => !item.streaming && item.path);
               if (!track) return 'no local track in the pool';
               openCueEditor(track);
               return track.key;
             })()
             """,
             4.0),
            ("cue editor drawn",
             """
             (() => {
               const panel = document.getElementById('cuePanel');
               if (panel.hidden) return 'panel closed';
               const canvas = document.getElementById('cueCanvas');
               const { data } = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height);
               let lit = 0;
               for (let index = 0; index < data.length; index += 16) {
                 if (data[index] + data[index + 1] + data[index + 2] > 60) lit += 1;
               }
               const plan = cueEditor.plan ? cueEditor.plan.hot_cues.length : 0;
               const band = cueEditor.wave ? cueEditor.wave.peaks_per_band : 0;
               return `${lit} lit px, plan ${plan} cues, ${band} peaks/band, ${cueEditor.error || 'no error'}`;
             })()
             """,
             0),
            // A click on the waveform has to seek to the point that was clicked.
            // This was worth pinning down: the pan branch used to carry the
            // length of the whole track instead of the position under the
            // pointer, so clicking the waveform played the last fraction of a
            // second and stopped.  The answer is the seek the page asked for
            // against the seek it should have asked for, both in seconds.
            ("cue click seeks",
             """
             (() => {
               const canvas = document.getElementById('cueCanvas');
               const rect = canvas.getBoundingClientRect();
               const want = 0.5;
               const point = {
                 clientX: rect.left + rect.width * want,
                 clientY: rect.top + rect.height / 2,
                 pointerId: 7, pointerType: 'mouse', isPrimary: true,
                 button: 0, buttons: 1, bubbles: true,
               };
               canvas.dispatchEvent(new PointerEvent('pointerdown', point));
               canvas.dispatchEvent(new PointerEvent('pointerup', { ...point, buttons: 0 }));
               const asked = player.seekOnLoad !== null && player.seekOnLoad !== undefined
                 ? player.seekOnLoad : player.el.currentTime;
               const expected = (cueAxisMs() / 1000) * want;
               return `${asked.toFixed(2)}/${expected.toFixed(2)}`;
             })()
             """,
             2.5),
            // And the marker has to be on the canvas, at the position playback is
            // actually at.  The answer is where the marker was drawn against
            // where the audio is, both as a fraction of the track.
            ("cue playhead",
             """
             (() => {
               const canvas = document.getElementById('cueCanvas');
               const rect = canvas.getBoundingClientRect();
               const dpr = window.devicePixelRatio || 1;
               const row = Math.round(60 * dpr);
               const { data } = canvas.getContext('2d').getImageData(0, row, canvas.width, 1);
               let column = -1;
               for (let px = 0; px < canvas.width; px += 1) {
                 const r = data[px * 4], g = data[px * 4 + 1], b = data[px * 4 + 2];
                 if (r > 190 && g > 190 && b > 190) { column = px / dpr; break; }
               }
               if (column < 0) return 'no playhead drawn';
               const axis = cueAxisMs() / 1000;
               return `${(column / rect.width).toFixed(3)}/${((player.el.currentTime || 0) / axis).toFixed(3)}`;
             })()
             """,
             0),
            // Pressing on the waveform used to clear the canvas and leave it
            // without a single cue flag on it, until the panel was reopened:
            // a pan drag carries no cue, and the pin drawing read the cue off it
            // anyway and threw part-way through the frame.  So: press, force the
            // redraw that a held pointer causes, and count the flags that are
            // actually on the canvas.  The answer is that count, and what a
            // failed draw would say instead.
            ("cue press keeps the cues",
             """
             (() => {
               const canvas = document.getElementById('cueCanvas');
               const rect = canvas.getBoundingClientRect();
               const point = {
                 clientX: rect.left + rect.width * 0.6,
                 clientY: rect.top + rect.height * 0.6,
                 pointerId: 9, pointerType: 'mouse', isPrimary: true,
                 button: 0, buttons: 1, bubbles: true,
               };
               canvas.dispatchEvent(new PointerEvent('pointerdown', point));
               let threw = '';
               try { drawCueEditor(); } catch (error) { threw = error.name; }
               const dpr = window.devicePixelRatio || 1;
               const { data } = canvas.getContext('2d').getImageData(0, Math.round(8 * dpr), canvas.width, 1);
               let flags = 0;
               let current = false;
               for (let px = 0; px < canvas.width; px += 1) {
                 const r = data[px * 4], g = data[px * 4 + 1], b = data[px * 4 + 2];
                 const lit = r + g + b > 150 && Math.max(r, g, b) - Math.min(r, g, b) > 30;
                 if (lit && !current) { flags += 1; }
                 current = lit;
               }
               canvas.dispatchEvent(new PointerEvent('pointerup', { ...point, buttons: 0 }));
               return `${flags} flags drawn on screen` + (threw ? `, draw threw ${threw}` : '');
             })()
             """,
             0.3),
            // The set tags, the portable package and the pre-flight check are the
            // three new ways a set leaves the app, and none of them exists unless
            // the menu entries and both tag toggles are wired up: an export tags
            // by default, a write into the library does not until it is asked.
            // This step also presses Build set, so the next two have a set to
            // check -- the check itself is a fetch, so it cannot be read back in
            // the same step.
            ("tag and package UI",
             """
             (() => {
               const menu = document.getElementById('exportMenu');
               const packageBtn = !!menu.querySelector('button[data-format="package"]');
               const checkBtn = !!menu.querySelector('button[data-format="preflight"]');
               const exportTags = document.getElementById('exportTags');
               const writeTags = document.getElementById('writeTags');
               const wired = typeof runPreflight === 'function'
                 && !!document.getElementById('preflightBody')
                 && !!document.getElementById('preflightPanel');
               document.getElementById('buildBtn').click();
               return `${packageBtn}/${checkBtn}/${wired}`
                 + `/${!!exportTags && exportTags.checked}/${!!writeTags && !writeTags.checked}`;
             })()
             """,
             4.0),
            // Every row of the built set carries the tag the export and the
            // write will put in the comment ([E7] [peak] 14/24), worked out by
            // the same server code rather than a second JS guess.  The previous
            // step built the set and its wait covers the tag request.
            ("set row tags",
             """
             (() => {
               const rows = [...document.querySelectorAll('#setList .set-row')];
               const shown = rows
                 .map((row) => row.querySelector('.set-tag'))
                 .filter((chip) => chip && !chip.hidden && chip.textContent.trim());
               const first = shown.length ? shown[0].textContent.trim() : '';
               const parts = first.split(' ');
               const roles = ['[opener]', '[build]', '[peak]', '[closer]'];
               const band = Number((parts[0] || '').replace('[E', '').replace(']', ''));
               const place = (parts[2] || '').split('/');
               const shape = parts.length === 3 && band >= 1 && band <= 10
                 && roles.includes(parts[1])
                 && place.length === 2 && Number(place[0]) >= 1 && Number(place[1]) >= 1;
               const planned = Object.keys(state.setTags || {}).length;
               return `${shown.length}/${rows.length} rows tagged, plan ${planned}, `
                 + `shape ${shape}, first ${first}`;
             })()
             """,
             0),
            ("pre-flight asked",
             """
             (() => {
               if (!state.set.length) return 'no set to check';
               runPreflight({ open: true });
               return `${state.set.length} tracks`;
             })()
             """,
             3.0),
            ("pre-flight panel",
             """
             (() => {
               const panel = document.getElementById('preflightPanel');
               if (panel.hidden) return 'panel did not open';
               const status = document.getElementById('preflightStatus').textContent.trim();
               const body = document.getElementById('preflightBody').textContent.split(/\\s+/).join(' ');
               const rows = panel.querySelectorAll('#preflightBody table tr').length;
               const explained = body.includes('blocks an export') || body.includes('all clear');
               return `${status}|${explained ? 'explained' : 'no explanation'}|${rows}`;
             })()
             """,
             0),
        ]
        var answers: [String: String] = [:]

        func step(_ index: Int) {
            guard index < steps.count else {
                let source = answers["source field"] ?? "(none)"
                let destination = answers["destination field"] ?? "(none)"
                let canvas = answers["cue editor drawn"] ?? "(none)"

                // The click and the marker, as "what happened / what it should be".
                let seek = (answers["cue click seeks"] ?? "").split(separator: "/").compactMap { Double($0) }
                let seekOk = seek.count == 2 && abs(seek[0] - seek[1]) <= max(1, seek[1] * 0.02)
                let head = (answers["cue playhead"] ?? "").split(separator: "/").compactMap { Double($0) }
                let headOk = head.count == 2 && abs(head[0] - head[1]) <= 0.02
                // Flags on the canvas while the pointer is down, and no throw.
                let press = answers["cue press keeps the cues"] ?? ""
                let flags = Int(press.prefix(while: { $0.isNumber })) ?? 0
                let pressOk = flags > 0 && !press.contains("threw")
                // The tag/package menu entries, then the pre-flight check of a
                // real set: a status line that names the count, a panel that
                // says what it means, and the wiring both tag toggles.
                let layout = (answers["tag and package UI"] ?? "").split(separator: "/").map(String.init)
                let asked = Int((answers["pre-flight asked"] ?? "").prefix(while: { $0.isNumber })) ?? 0
                let panel = answers["pre-flight panel"] ?? ""
                // The status line reads either "all clear - N tracks checked" or
                // "N error(s), N warning(s) in N tracks", and either way the
                // panel body has to say what it means for the export.
                let panelStatus = panel.split(separator: "|").first.map(String.init) ?? ""
                let panelOk = (panelStatus.contains("checked")
                    || panelStatus.contains("error(s)")
                    || panelStatus.contains("warning(s)"))
                    && panel.contains("explained")
                let preflightOk = layout == ["true", "true", "true", "true", "true"]
                    && asked > 0
                    && panelOk
                // The tags: every row of the set carries one, it is the tag the
                // plan holds, and it reads as [E7] [peak] 14/24.
                let tagAnswer = answers["set row tags"] ?? ""
                let tagCounts = tagAnswer.split(separator: " ").first.map(String.init) ?? ""
                let taggedRows = Int(tagCounts.split(separator: "/").first.map(String.init) ?? "") ?? 0
                let setRows = Int(tagCounts.split(separator: "/").last.map(String.init) ?? "") ?? 0
                let tagsOk = taggedRows > 0 && taggedRows == setRows
                    && tagAnswer.contains("shape true")

                let ok = source == "/tmp/parity-pick-source"
                    && destination == "/tmp/parity-pick-destination"
                    && answers["xml drop-in fallback"] == "true"
                    && answers["cue editor present"] == "true"
                    // A draw that threw would leave no answer at all.
                    && canvas.first?.isNumber == true
                    && canvas.contains("peaks/band")
                    && seekOk
                    && headOk
                    && pressOk
                    && preflightOk
                    && tagsOk
                log.write("selftest result: shell=\(answers["shell"] ?? "(none)")"
                    + " xmlDrop=\(answers["xml drop-in fallback"] ?? "false")"
                    + " ok=\(ok) picker=\(source) / \(destination)")
                log.write("selftest result: cue canvas: \(canvas)")
                log.write("selftest result: cue click seeks \(answers["cue click seeks"] ?? "(none)")"
                    + " (asked/should be, seconds), playhead \(answers["cue playhead"] ?? "(none)")"
                    + " (drawn/position, of the track)")
                log.write("selftest result: cue press keeps the cues: "
                    + "\(answers["cue press keeps the cues"] ?? "(none)")")
                log.write("selftest result: tag and package UI: "
                    + "\(answers["tag and package UI"] ?? "(none)")"
                    + " (package/preflight/wired/export tags on/write tags off)")
                log.write("selftest result: set row tags: \(tagAnswer.isEmpty ? "(none)" : tagAnswer)")
                log.write("selftest result: pre-flight: \(answers["pre-flight asked"] ?? "(none)")"
                    + " asked, panel: \(panel.isEmpty ? "(none)" : panel)")
                stopOwnedServer()
                exit(ok ? 0 : 1)
            }
            let (label, script, wait) = steps[index]
            webView.evaluateJavaScript(script) { value, error in
                if let error {
                    self.log.write("selftest: \(label) failed: \(error.localizedDescription)")
                }
                let answer = value.map { "\($0)" } ?? "(none)"
                self.log.write("selftest: \(label) = \(answer)")
                answers[label] = answer
                DispatchQueue.main.asyncAfter(deadline: .now() + wait) { step(index + 1) }
            }
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { step(0) }
    }

    // MARK: menu

    private func buildMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Parity",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Parity", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Parity", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu

        let viewItem = NSMenuItem()
        mainMenu.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(withTitle: "Reload", action: #selector(reload(_:)), keyEquivalent: "r")
        viewMenu.addItem(withTitle: "Actual Size", action: #selector(zoomReset(_:)), keyEquivalent: "0")
        viewMenu.addItem(withTitle: "Zoom In", action: #selector(zoomIn(_:)), keyEquivalent: "+")
        viewMenu.addItem(withTitle: "Zoom Out", action: #selector(zoomOut(_:)), keyEquivalent: "-")
        viewItem.submenu = viewMenu

        NSApp.mainMenu = mainMenu
    }

    @objc private func reload(_ sender: Any?) { webView.reload() }
    @objc private func zoomReset(_ sender: Any?) {
        webView.pageZoom = 1.0
    }
    @objc private func zoomIn(_ sender: Any?) {
        webView.pageZoom = min(2.0, webView.pageZoom + 0.1)
    }
    @objc private func zoomOut(_ sender: Any?) {
        webView.pageZoom = max(0.6, webView.pageZoom - 0.1)
    }
}

// MARK: - entry point

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
