import SwiftUI
import UIKit

/// The first thing you see: what Little Toby is, and pairing a computer.
struct WelcomeView: View {
    @State private var pairing = false

    var body: some View {
        VStack(spacing: 20) {
            Spacer()
            TobyFace(mood: .happy, size: 170)
            VStack(spacing: 8) {
                Text("Little Toby").font(.largeTitle.bold()).foregroundStyle(Theme.inkBright)
                Text("Your computer, from anywhere.")
                    .font(.title3).foregroundStyle(Theme.inkNormal)
            }
            VStack(alignment: .leading, spacing: 14) {
                Feature(symbol: "bubble.left.and.text.bubble.right",
                        text: "Tell Toby what to do. It does it on your computer while you're away.")
                Feature(symbol: "checklist", text: "Watch each step live, and see what it found.")
                Feature(symbol: "hand.raised", text: "Anything that changes something asks you first.")
                Feature(symbol: "lock.shield", text: "Private: only your own devices can reach it, through Tailscale.")
            }
            .padding(.horizontal, 8)
            Spacer()
            Button("Pair a computer") { pairing = true }
                .buttonStyle(TobyButtonStyle())
                .accessibilityIdentifier("welcome.pair")
            Text("You'll need Little Toby running on the computer, with its phone connection on (toby phone on).")
                .font(.caption).foregroundStyle(Theme.inkQuiet).multilineTextAlignment(.center)
        }
        .padding(24)
        .background(Theme.background.ignoresSafeArea())
        .sheet(isPresented: $pairing) { PairingView() }
    }
}

private struct Feature: View {
    var symbol: String
    var text: String

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: symbol).font(.title3).foregroundStyle(Theme.accent).frame(width: 28)
            Text(text).font(.subheadline).foregroundStyle(Theme.inkNormal)
        }
    }
}

/// Pair with a computer: scan its code (or type it), check both screens
/// show the same number, approve on the computer.
struct PairingView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var flow = PairingFlow()
    @State private var scanning = false
    @State private var typing = UITestSupport.isActive
    @State private var address = UITestSupport.server ?? ""
    @State private var code = ""
    @State private var deviceName = UIDevice.current.name
    @State private var scanError: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 20) {
                    switch flow.step {
                    case .idle, .failed:
                        start
                    case .claiming:
                        ProgressView("Checking the code…").padding(.top, 80)
                    case .waiting(let compare, let computerName):
                        waiting(compare: compare, computerName: computerName)
                    case .paired(let computerName):
                        paired(computerName: computerName)
                    }
                }
                .padding(20)
                .animation(Motion.enter, value: flow.step)
            }
            .background(Theme.background.ignoresSafeArea())
            .navigationTitle("Pair a computer")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        flow.cancel()
                        dismiss()
                    }
                }
            }
            .sheet(isPresented: $scanning) {
                QRScannerSheet { text in
                    scanning = false
                    if let link = PairingLink.parse(text) {
                        begin(link)
                    } else {
                        scanError = "That QR code isn't a Little Toby pairing code."
                    }
                }
            }
        }
        .interactiveDismissDisabled(isWaiting)
    }

    private var isWaiting: Bool {
        if case .waiting = flow.step { return true }
        return false
    }

    @ViewBuilder private var start: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("On your computer").font(.headline).foregroundStyle(Theme.inkBright)
            Text("Open Toby's Settings and choose Connect a phone, or run toby phone pair in a terminal. It shows a QR code and an eight-character code.")
                .font(.subheadline).foregroundStyle(Theme.inkNormal)
        }
        .card()

        if case .failed(let message) = flow.step {
            Label(message, systemImage: "exclamationmark.triangle")
                .font(.subheadline).foregroundStyle(Theme.warn).card()
                .accessibilityIdentifier("pair.error")
        }
        if let scanError {
            Label(scanError, systemImage: "qrcode").font(.subheadline).foregroundStyle(Theme.warn).card()
        }

        VStack(alignment: .leading, spacing: 6) {
            Text("This phone's name").font(.caption).foregroundStyle(Theme.inkQuiet)
            TextField("iPhone", text: $deviceName)
                .textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("pair.name")
        }

        Button {
            scanError = nil
            scanning = true
        } label: {
            Label("Scan the QR code", systemImage: "qrcode.viewfinder")
        }
        .buttonStyle(TobyButtonStyle())
        .accessibilityIdentifier("pair.scan")

        if typing {
            VStack(alignment: .leading, spacing: 10) {
                TextField("Address, e.g. laptop.tail1234.ts.net", text: $address)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityIdentifier("pair.address")
                TextField("Code, e.g. K7M4-XQ2P", text: $code)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .font(.body.monospaced())
                    .textFieldStyle(.roundedBorder)
                    .accessibilityIdentifier("pair.code")
                Button("Pair") {
                    guard let base = PairingLink.baseURL(fromTyped: address) else {
                        scanError = "That address doesn't look right."
                        return
                    }
                    let clean = PairingLink.normalize(code)
                    guard clean.count == 8 else {
                        scanError = "The code is eight letters and numbers, like K7M4-XQ2P."
                        return
                    }
                    begin(PairingLink(baseURL: base, code: clean))
                }
                .buttonStyle(TobyButtonStyle(kind: .secondary))
                .accessibilityIdentifier("pair.submit")
            }
            .card()
            .transition(.arrive)
        } else {
            Button("Type the address and code instead") {
                withAnimation(Motion.enter) { typing = true }
            }
            .font(.subheadline)
            .accessibilityIdentifier("pair.manual")
        }
    }

    private func waiting(compare: String, computerName: String) -> some View {
        VStack(spacing: 18) {
            TobyFace(mood: .waiting, size: 110)
            Text("Approve on \(computerName)")
                .font(.title2.bold()).foregroundStyle(Theme.inkBright).multilineTextAlignment(.center)
            Text("\(computerName) is asking whether to pair with this phone. Check it shows the same number, then say yes there.")
                .font(.subheadline).foregroundStyle(Theme.inkNormal).multilineTextAlignment(.center)
            Text(String(compare.prefix(3)) + " " + String(compare.suffix(3)))
                .font(.system(size: 44, weight: .bold, design: .rounded).monospacedDigit())
                .tracking(4)
                .foregroundStyle(Theme.inkBright)
                .padding(.vertical, 8)
                .accessibilityIdentifier("pair.compare")
            ProgressView()
            Text("If the numbers are different, say no on the computer.")
                .font(.caption).foregroundStyle(Theme.inkQuiet)
        }
        .padding(.top, 20)
    }

    private func paired(computerName: String) -> some View {
        VStack(spacing: 18) {
            TobyFace(mood: .celebrating, size: 120)
            Text("Paired with \(computerName)")
                .font(.title2.bold()).foregroundStyle(Theme.inkBright).multilineTextAlignment(.center)
            Text("You can reach it from anywhere your phone has internet, as long as Tailscale is on.")
                .font(.subheadline).foregroundStyle(Theme.inkNormal).multilineTextAlignment(.center)
            Button("Done") { dismiss() }
                .buttonStyle(TobyButtonStyle())
                .accessibilityIdentifier("pair.done")
        }
        .padding(.top, 20)
        .sensoryFeedback(.success, trigger: computerName)
    }

    private func begin(_ link: PairingLink) {
        let name = deviceName.trimmingCharacters(in: .whitespaces).isEmpty ? "iPhone" : deviceName
        flow.start(link: link, deviceName: name, store: model.computers) { computer in
            model.paired(computer)
        }
    }
}
