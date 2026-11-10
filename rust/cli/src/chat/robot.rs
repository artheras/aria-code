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

    /// Two rows by eight columns: the supplied isometric mark, not a face.
    /// Each cell is one terminal row of half-blocks, so the picture is four
    /// pixels tall. The top row is the roof with the opening; the bottom row
    /// is the left window and the right slit. Cream on a dark terminal, ink
    /// on a light one. No eyes, ears, or feet.
    fn compact_cream(theme: &str) -> Self {
        let ink = if theme == "light" {
            Color::Rgb(37, 39, 34)
        } else {
            Color::Rgb(247, 246, 240)
        };
        // Four pixel rows. `true` is the mark, `false` is the opening.
        let pixels: [[bool; 8]; 4] = [
            [false, true, true, true, true, true, true, false],
            [true, true, false, true, true, false, true, true],
            [true, false, true, true, false, false, true, true],
            [false, true, true, true, true, true, true, false],
        ];
        let solid = Span::styled("▀", Style::new().fg(ink).bg(ink));
        let mut rows = Vec::new();
        for pair in pixels.chunks(2) {
            let mut spans = Vec::new();
            for col in 0..8 {
                let upper = pair[0][col];
                let lower = pair[1][col];
                spans.push(match (upper, lower) {
                    (true, true) => solid.clone(),
                    (true, false) => Span::styled("▀", Style::new().fg(ink)),
                    (false, true) => Span::styled("▄", Style::new().fg(ink)),
                    (false, false) => Span::raw(" "),
                });
            }
            rows.push(Line::from(spans));
        }
        Self(rows)
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
            assert_eq!(Robot::cream(theme, true).0.len(), 2);
        }
    }
    #[test]
    fn compact_mark_is_the_isometric_silhouette() {
        for (theme, ink) in [
            ("light", Color::Rgb(37, 39, 34)),
            ("dark", Color::Rgb(247, 246, 240)),
        ] {
            let robot = Robot::from_event(&serde_json::json!({
                "design": "cream-block-v2", "theme": theme
            }));
            assert_eq!(robot.0.len(), 2);
            assert!(robot.0.iter().all(|row| row.width() == 8));
            // Roof opening on the left of the first row, window on the second.
            assert_eq!(robot.0[0].spans[0].content, "▄");
            assert_eq!(robot.0[0].spans[2].content, "▀");
            assert_eq!(robot.0[1].spans[1].content, "▄");
            assert_eq!(robot.0[1].spans[4].content, "▄");
            assert_eq!(robot.0[0].spans[1].style.fg, Some(ink));
            let drawn: String = robot
                .0
                .iter()
                .flat_map(|row| row.spans.iter().map(|span| span.content.as_ref()))
                .collect();
            assert!(!drawn.contains('▗') && !drawn.contains('▖'));
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
