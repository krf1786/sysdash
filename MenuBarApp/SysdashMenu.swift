import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let sysdashDir = "/Users/kylefleming/sysdash"
    private let agentLabel = "com.sysdash.agent"
    private var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        if let button = statusItem.button {
            button.title = "SD"
            button.image = nil
            button.imagePosition = .imageLeading
        }

        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Open Sysdash", action: #selector(openSysdash), keyEquivalent: "o"))
        menu.addItem(NSMenuItem(title: "Restart Sysdash", action: #selector(restartSysdash), keyEquivalent: "r"))
        menu.addItem(NSMenuItem(title: "Open Folder", action: #selector(openFolder), keyEquivalent: "f"))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit Menu Bar Helper", action: #selector(quit), keyEquivalent: "q"))
        menu.items.forEach { $0.target = self }
        statusItem.menu = menu

        timer = Timer.scheduledTimer(withTimeInterval: 8, repeats: true) { [weak self] _ in
            self?.refreshStatus()
        }
        refreshStatus()
    }

    @objc private func openSysdash() {
        ensureAgent()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            NSWorkspace.shared.open(URL(string: self.dashboardURL())!)
        }
    }

    @objc private func restartSysdash() {
        run("/bin/launchctl", ["kickstart", "-k", "gui/\(getuid())/\(agentLabel)"])
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
            self.openSysdash()
        }
    }

    @objc private func openFolder() {
        NSWorkspace.shared.open(URL(fileURLWithPath: sysdashDir))
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private func refreshStatus() {
        let live = isListening()
        statusItem.button?.contentTintColor = live ? NSColor.systemGreen : NSColor.systemOrange
        statusItem.button?.toolTip = live ? "sysdash is running" : "sysdash is not responding"
    }

    private func ensureAgent() {
        let domain = "gui/\(getuid())/\(agentLabel)"
        let result = run("/bin/launchctl", ["print", domain])
        if result != 0 {
            _ = run("/bin/bash", ["\(sysdashDir)/install-autostart.sh"])
        } else {
            _ = run("/bin/launchctl", ["kickstart", "-k", domain])
        }
    }

    private func dashboardURL() -> String {
        let portPath = NSString(string: "~/.sysdash-port").expandingTildeInPath
        if let port = try? String(contentsOfFile: portPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
           !port.isEmpty {
            return "http://localhost:\(port)/"
        }
        return "http://localhost:55067/"
    }

    private func isListening() -> Bool {
        let portPath = NSString(string: "~/.sysdash-port").expandingTildeInPath
        guard let port = try? String(contentsOfFile: portPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
              !port.isEmpty else { return false }
        return run("/usr/bin/nc", ["-z", "127.0.0.1", port]) == 0
    }

    @discardableResult
    private func run(_ launchPath: String, _ args: [String]) -> Int32 {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: launchPath)
        task.arguments = args
        task.currentDirectoryURL = URL(fileURLWithPath: sysdashDir)
        task.standardOutput = Pipe()
        task.standardError = Pipe()
        do {
            try task.run()
            task.waitUntilExit()
            return task.terminationStatus
        } catch {
            return 1
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
