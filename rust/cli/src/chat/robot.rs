//! Native cream-block mascot. Legacy span packets remain readable during
//! migration; the new runtime names a design instead of drawing it in Python.
use ratatui::{
    style::{Color, Style},
    text::{Line, Span},
};
use serde_json::Value;

#[derive(Debug, Default)]
pub struct Robot(pub Vec<Line<'static>>);
fn color(hex: &str) -> Option<Color> {
    let rgb = hex.strip_prefix('#')?;
    if rgb.len() != 6 || !rgb.is_ascii() {
        return None;
    }
    let rgb = u32::from_str_radix(rgb, 16).ok()?;
    Some(Color::Rgb((rgb >> 16) as u8, (rgb >> 8) as u8, rgb as u8))
}
impl Robot {
    pub fn from_event(value: &Value) -> Self {
        if value["design"] == "cream-block-v2" {
            return Self::cream(value["theme"].as_str().unwrap_or("dark"), true);
        }
        let Some(rows) = value.as_array() else {
            return Self::default();
        };
        if rows.len() != 4 {
            return Self::default();
        }
        let mut lines = Vec::new();
        for row in rows {
            let Some(spans) = row.as_array() else {
                return Self::default();
            };
            if spans.len() > 16 {
                return Self::default();
            }
            let mut out = Vec::new();
            for span in spans {
                let Some(text) = span["text"].as_str() else {
                    return Self::default();
                };
                if text.chars().count() > 9 || text.chars().any(char::is_control) {
                    return Self::default();
                }
                let mut style = Style::default();
                let mut background = false;
                for part in span["style"].as_str().unwrap_or("").split_whitespace() {
                    if part == "on" {
                        background = true;
                        continue;
                    }
                    if let Some(color) = color(part) {
                        style = if background {
                            style.bg(color)
                        } else {
                            style.fg(color)
                        };
                    }
                }
                out.push(Span::styled(text.to_owned(), style));
            }
            let line = Line::from(out);
            if line.width() != 9 {
                return Self::default();
            }
            lines.push(line);
        }
        Self(lines)
    }

    pub fn cream(theme: &str, compact: bool) -> Self {
        if compact {
            return Self::compact_cream(theme);
        }
        let body = if theme == "light" {
            Color::Rgb(232, 223, 213)
        } else {
            Color::Rgb(244, 235, 228)
        };
        let face = Color::Rgb(14, 14, 14);
        let eye = Color::Rgb(255, 253, 245);
        let orange = Color::Rgb(249, 180, 103);
        let shadow = Color::Rgb(156, 148, 139);
        let leg = Color::Rgb(213, 204, 195);
        let (width, height) = (22, 18);
        let mut pixels = vec![vec![None; width]; height];
        let mut fill = |x: std::ops::Range<usize>, y: std::ops::Range<usize>, color| {
            for row in pixels.iter_mut().take(y.end).skip(y.start) {
                for pixel in row.iter_mut().take(x.end).skip(x.start) {
                    *pixel = Some(color);
                }
            }
        };
        // Stepped cap, inset black screen, two matching white eyes, amber ears.
        fill(3..19, 2..14, body);
        fill(4..18, 1..2, body);
        fill(5..17, 3..11, face);
        fill(1..3, 6..10, shadow);
        fill(19..21, 6..10, shadow);
        fill(2..3, 7..8, orange);
        fill(19..20, 7..8, orange);
        fill(7..9, 6..8, eye);
        fill(13..15, 6..8, eye);
        fill(3..19, 14..15, shadow);
        for col in [4, 8, 12, 16] {
            fill(col..col + 2, 15..18, leg);
            fill(col..col + 2, 17..18, shadow);
        }
        let stripe = 6;
        let stripe_columns = 3..19;
        let mut rows = Vec::new();
        for row in (0..height).step_by(2) {
            let mut spans = Vec::new();
            for (col, (&upper, &lower)) in pixels[row].iter().zip(&pixels[row + 1]).enumerate() {
                let (glyph, style) = match (upper, lower) {
                    (Some(a), Some(b)) if a == b => (" ", Style::new().bg(a)),
                    (Some(a), Some(b)) => ("▀", Style::new().fg(a).bg(b)),
                    (Some(a), None) => ("▀", Style::new().fg(a)),
                    (None, Some(b)) => ("▄", Style::new().fg(b)),
                    _ => (" ", Style::new()),
                };
                spans.push(if row / 2 == stripe && stripe_columns.contains(&col) {
                    // A one-eighth block keeps the bottom orange detail thin,
                    // instead of filling half a terminal row with orange.
                    Span::styled("▁", Style::new().fg(orange).bg(body))
                } else {
                    Span::styled(glyph, style)
                });
            }
            rows.push(Line::from(spans));
        }
        Self(rows)
    }

    /// The compact mascot: 9 columns by 4 rows, the height of the four lines of
    /// text beside it on the welcome screen. It follows the artwork's face: an
    /// ivory square left eye and an amber dash right eye on the black screen.
    ///
    /// Terminals paint a cell's background over the whole line box but draw a
    /// block glyph only inside the font's em box, so with line spacing above 1.0
    /// a background shows as a stripe at the cell's top and bottom. Each cell
    /// therefore takes as its background the colour that continues across the
    /// row seam inside the robot (cream for the cap and sides, black for the
    /// screen) and draws the other colour as the glyph; the outline is
    /// foreground only. The one seam that cannot be continuous, under the
    /// screen, falls between the body and its base, where the artwork has a
    /// seam line of its own.
    fn compact_cream(theme: &str) -> Self {
        let (body, ear, leg) = if theme == "light" {
            (
                Color::Rgb(230, 221, 208),
                Color::Rgb(156, 148, 139),
                Color::Rgb(184, 175, 166),
            )
        } else {
            (
                Color::Rgb(244, 235, 228),
                Color::Rgb(182, 173, 164),
                Color::Rgb(213, 204, 195),
            )
        };
        let screen = Color::Rgb(14, 14, 14);
        let eye = Color::Rgb(255, 253, 245);
        let amber = Color::Rgb(249, 180, 103);
        let fg = |text: &'static str, color: Color| Span::styled(text, Style::new().fg(color));
        let bg = |text: &'static str, color: Color| Span::styled(text, Style::new().bg(color));
        let on =
            |text: &'static str, f: Color, b: Color| Span::styled(text, Style::new().fg(f).bg(b));
        let rows = vec![
            // Cap: cream throughout, so its seam with the face row is cream.
            vec![fg("▗", body), bg("       ", body), fg("▖", body)],
            // Ears, screen top, square eye, gap, dash eye.
            vec![
                fg("▐", ear),
                on("▗", screen, body),
                on("▗▖", eye, screen),
                bg(" ", screen),
                on("▂", amber, screen),
                bg(" ", screen),
                on("▖", screen, body),
                fg("▌", ear),
            ],
            // Screen bottom over the body: black above, cream below.
            vec![
                fg("▐", body),
                on("▝", screen, body),
                on("▄▄▄▄▄", body, screen),
                on("▘", screen, body),
                fg("▌", body),
            ],
            // Base and four feet.
            vec![
                fg(" ", ear),
                fg("▀", ear),
                on("▀", ear, leg),
                fg("▀", ear),
                on("▀", ear, leg),
                fg("▀", ear),
                on("▀", ear, leg),
                fg("▀", ear),
                fg(" ", ear),
            ],
        ];
        Self(rows.into_iter().map(Line::from).collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_reference_has_four_separate_feet_and_thin_amber_detail() {
        for theme in ["light", "dark"] {
            let robot = Robot::cream(theme, false);
            assert_eq!(robot.0.len(), 9);
            assert!(robot.0.iter().all(|row| row.width() == 22));
            let feet = &robot.0[8].spans;
            let shadow = Some(Color::Rgb(156, 148, 139));
            let mut groups = 0;
            let mut inside = false;
            for span in feet {
                let leg = span.style.bg == shadow;
                if leg && !inside {
                    groups += 1;
                }
                inside = leg;
            }
            assert_eq!(groups, 4);
            assert_eq!(robot.0[6].spans[3].content, "▁");
            assert_eq!(
                robot.0[6].spans[3].style.fg,
                Some(Color::Rgb(249, 180, 103))
            );
            assert_eq!(
                robot.0[3].spans[7].style.bg,
                Some(Color::Rgb(255, 253, 245))
            );
            assert_eq!(Robot::cream(theme, true).0.len(), 4);
        }
    }
    #[test]
    fn compact_mascot_has_the_artworks_wink_and_four_feet() {
        let ivory = Some(Color::Rgb(255, 253, 245));
        let amber = Some(Color::Rgb(249, 180, 103));
        let black = Some(Color::Rgb(14, 14, 14));
        for theme in ["light", "dark"] {
            let robot = Robot::from_event(&serde_json::json!({
                "design": "cream-block-v2", "theme": theme
            }));
            // As tall as the four lines of text beside it, and 9 columns wide.
            assert_eq!(robot.0.len(), 4);
            assert!(robot.0.iter().all(|row| row.width() == 9));
            // The face: an ivory square left eye and an amber dash right eye,
            // both on the black screen, one gap apart.
            let face = &robot.0[1].spans;
            let eye = face.iter().find(|s| s.content == "▗▖").unwrap();
            assert_eq!((eye.style.fg, eye.style.bg), (ivory, black));
            let dash = face.iter().find(|s| s.content == "▂").unwrap();
            assert_eq!((dash.style.fg, dash.style.bg), (amber, black));
            // Four feet: alternating filled and open cells along the base.
            let feet: Vec<bool> = robot.0[3]
                .spans
                .iter()
                .map(|s| s.style.bg.is_some())
                .collect();
            assert_eq!(
                feet,
                [false, false, true, false, true, false, true, false, false]
            );
        }
    }

    /// With line spacing above 1.0 a cell's background shows above and below
    /// its glyph. The cap row must not carry the screen's black (the stripe
    /// that showed over the robot's head), and the outline cells carry no
    /// background at all.
    #[test]
    fn compact_mascot_backgrounds_do_not_stripe_with_tall_line_spacing() {
        let black = Some(Color::Rgb(14, 14, 14));
        for theme in ["light", "dark"] {
            let robot = Robot::cream(theme, true);
            assert!(robot.0[0].spans.iter().all(|s| s.style.bg != black));
            for row in &robot.0 {
                let first = row.spans.first().unwrap();
                let last = row.spans.last().unwrap();
                assert_eq!(first.style.bg, None);
                assert_eq!(last.style.bg, None);
            }
        }
    }
    #[test]
    fn spans_keep_exact_glyphs_and_foreground_background() {
        let value = serde_json::json!([
            [{"style":"#F4EBE4 on #0E0E0E", "text":"▗▛▀▀▀▀▀▜▖"}],
            [{"style":"#F9B467 on #0E0E0E", "text":"▌▌▗▖ ▗▖▐▐"}],
            [{"style":"on #0E0E0E", "text":"▐▙▄▄▄▄▄▟▌"}],
            [{"style":"#B6ADA4 on #D5CCC3", "text":"▝▀▀▀▀▀▀▀▘"}]
        ]);
        let robot = Robot::from_event(&value);
        assert_eq!(robot.0.len(), 4);
        assert_eq!(robot.0[1].spans[0].content, "▌▌▗▖ ▗▖▐▐");
        assert_eq!(
            robot.0[1].spans[0].style.fg,
            Some(Color::Rgb(249, 180, 103))
        );
        assert_eq!(robot.0[1].spans[0].style.bg, Some(Color::Rgb(14, 14, 14)));
        assert_eq!(robot.0[2].spans[0].style.fg, None);
        assert!(
            Robot::from_event(&serde_json::json!([[{"text":"\u{001b}"}]]))
                .0
                .is_empty()
        );
    }
}
