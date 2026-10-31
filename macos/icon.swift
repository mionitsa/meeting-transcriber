// Draws the app icon: a waveform and microphone on a rounded gradient square.
// usage: swift icon.swift out.png   (1024 x 1024 PNG)
import AppKit
let size: CGFloat = 1024
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
let inset: CGFloat = 100
let rect = NSRect(x: inset, y: inset, width: size - 2*inset, height: size - 2*inset)
let path = NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185)
NSGraphicsContext.current?.saveGraphicsState()
let shadow = NSShadow(); shadow.shadowBlurRadius = 30; shadow.shadowOffset = NSSize(width: 0, height: -12)
shadow.shadowColor = NSColor.black.withAlphaComponent(0.3); shadow.set()
NSColor.black.setFill(); path.fill()
NSGraphicsContext.current?.restoreGraphicsState()
let grad = NSGradient(starting: NSColor(calibratedRed: 0.36, green: 0.30, blue: 0.95, alpha: 1),
                      ending: NSColor(calibratedRed: 0.10, green: 0.62, blue: 0.95, alpha: 1))!
grad.draw(in: path, angle: -60)
let cfg = NSImage.SymbolConfiguration(pointSize: 470, weight: .semibold)
    .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
if let sym = NSImage(systemSymbolName: "waveform.and.mic", accessibilityDescription: nil)?.withSymbolConfiguration(cfg) {
    let s = sym.size
    sym.draw(in: NSRect(x: (size - s.width)/2, y: (size - s.height)/2, width: s.width, height: s.height))
}
img.unlockFocus()
let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
