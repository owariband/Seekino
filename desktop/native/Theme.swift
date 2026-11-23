import SwiftUI

struct Celadon {
    private let dark: Bool
    init(_ scheme: ColorScheme) { dark = scheme == .dark }

    var canvas: Color { pair(0xe4eae5, 0x0d1110) }
    var surface: Color { pair(0xebf0ec, 0x171c1a) }
    var paper: Color { pair(0xf6f8f5, 0x202624) }
    var muted: Color { pair(0xd9e1db, 0x29312e) }
    var ink: Color { pair(0x17312b, 0xdde5e1) }
    var secondary: Color { pair(0x566b65, 0xa7b5ae) }
    var accent: Color { pair(0x126b5d, 0x8dd3c8) }
    var wash: Color { pair(0xd9eee7, 0x213330) }
    var line: Color { pair(0x244940, 0xa0cdc0).opacity(0.15) }
    var danger: Color { pair(0x9c443d, 0xf28b82) }
    var stage: Color { pair(0x0c1412, 0x080d0c) }
    var buttonInk: Color { pair(0xffffff, 0x0b2520) }

    private func pair(_ day: Int, _ night: Int) -> Color {
        let color = dark ? night : day
        return Color(red: Double((color >> 16) & 255) / 255, green: Double((color >> 8) & 255) / 255,
                     blue: Double(color & 255) / 255)
    }
}

struct ActionStyle: ButtonStyle {
    enum Kind { case primary, secondary }
    @Environment(\.colorScheme) private var scheme
    @Environment(\.isEnabled) private var enabled
    var kind: Kind = .secondary

    func makeBody(configuration: Configuration) -> some View {
        let theme = Celadon(scheme)
        return configuration.label
            .font(.system(size: 13, weight: .semibold))
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            .foregroundStyle(kind == .primary ? theme.buttonInk : theme.ink)
            .background(kind == .primary ? theme.accent : theme.paper, in: RoundedRectangle(cornerRadius: 9))
            .overlay { RoundedRectangle(cornerRadius: 9).strokeBorder(theme.line) }
            .opacity(enabled ? (configuration.isPressed ? 0.72 : 1) : 0.46)
    }
}

struct Surface: ViewModifier {
    @Environment(\.colorScheme) private var scheme

    func body(content: Content) -> some View {
        let theme = Celadon(scheme)
        content.background(theme.paper, in: RoundedRectangle(cornerRadius: 14))
            .overlay { RoundedRectangle(cornerRadius: 14).strokeBorder(theme.line) }
    }
}

struct EmptyPane: View {
    @Environment(\.colorScheme) private var scheme
    let title: String
    let detail: String
    let icon: String

    var body: some View {
        let theme = Celadon(scheme)
        VStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 26, weight: .medium))
                .foregroundStyle(theme.accent)
                .frame(width: 58, height: 58)
                .background(theme.wash, in: RoundedRectangle(cornerRadius: 17))
                .accessibilityHidden(true)
            Text(title).font(.system(size: 18, weight: .semibold)).foregroundStyle(theme.ink)
            Text(detail).font(.system(size: 13)).foregroundStyle(theme.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(22)
        .frame(maxWidth: .infinity)
    }
}

struct StatusTag: View {
    @Environment(\.colorScheme) private var scheme
    let title: String
    var failure = false

    var body: some View {
        let theme = Celadon(scheme)
        Text(title).font(.system(size: 11, weight: .medium))
            .foregroundStyle(failure ? theme.danger : theme.accent)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(failure ? theme.danger.opacity(0.08) : theme.wash, in: Capsule())
    }
}

struct VideoCover: View {
    @Environment(\.colorScheme) private var scheme
    let path: String?
    @State private var image: NSImage?

    var body: some View {
        let theme = Celadon(scheme)
        GeometryReader { size in
            ZStack {
                LinearGradient(colors: [theme.wash, theme.muted], startPoint: .topLeading, endPoint: .bottomTrailing)
                if let image {
                    Image(nsImage: image).resizable().scaledToFill()
                        .frame(width: size.size.width, height: size.size.height)
                } else {
                    Image(systemName: "film").font(.system(size: 23, weight: .light)).foregroundStyle(theme.accent)
                }
            }
            .clipped()
        }
        .clipShape(RoundedRectangle(cornerRadius: 9))
        .accessibilityHidden(true)
        .task(id: path) {
            image = nil
            guard let path else { return }
            let data = await Task.detached { try? Data(contentsOf: URL(fileURLWithPath: path)) }.value
            if !Task.isCancelled, let data { image = NSImage(data: data) }
        }
    }
}
