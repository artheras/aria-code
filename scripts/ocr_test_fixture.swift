// Generate a deterministic multilingual OCR integration fixture on macOS.
import AppKit
guard CommandLine.arguments.count == 2 else { fatalError("usage: swift ocr_test_fixture.swift output.png") }
let image = NSImage(size: NSSize(width: 1500, height: 500))
image.lockFocus()
NSColor.white.setFill()
NSRect(x: 0, y: 0, width: 1500, height: 500).fill()
let style: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 64), .foregroundColor: NSColor.black]
("中文识别测试 数据分析 2026\n繁體中文 測試 報告\nOCR EVIDENCE 12345" as NSString).draw(in: NSRect(x: 60, y: 70, width: 1380, height: 350), withAttributes: style)
image.unlockFocus()
let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
