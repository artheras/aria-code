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

    /// A small, independently pixel-fitted rendition of docs/assets/aria-robot.png.
    /// Quadrants keep the feet wider than a single column. The eyes use the
    /// same baseline, with two identical square ivory eyes.
    fn compact_cream(theme: &str) -> Self {
        let body = if theme == "light" {
            Color::Rgb(241, 233, 223)
        } else {
            Color::Rgb(244, 235, 228)
        };
        let face = Color::Rgb(14, 14, 14);
        let eye = Color::Rgb(255, 253, 245);
        let orange = Color::Rgb(249, 180, 103);
        let foot = Color::Rgb(207, 196, 183);
        let ear = Color::Rgb(174, 164, 150);
        let mut cells = vec![vec![Span::raw(" "); 14]; 6];
        for cell in cells[0].iter_mut().take(12).skip(2) {
            *cell = Span::styled("▄", Style::new().fg(body));
        }
        for row in cells.iter_mut().take(5).skip(1) {
            for cell in row.iter_mut().take(13).skip(1) {
                *cell = Span::styled(" ", Style::new().bg(body));
            }
        }
        // Half-column frame edges preserve the inset screen without making
        // the cream frame thicker than the eye, or rounding away the face.
        for row in cells.iter_mut().take(4).skip(1) {
            row[2] = Span::styled("▌", Style::new().fg(body).bg(face));
            row[11] = Span::styled("▐", Style::new().fg(body).bg(face));
            for cell in row.iter_mut().take(11).skip(3) {
                *cell = Span::styled(" ", Style::new().bg(face));
            }
        }
        for col in [4, 5] {
            cells[2][col] = Span::styled(" ", Style::new().bg(eye));
        }
        for col in [8, 9] {
            cells[2][col] = Span::styled(" ", Style::new().bg(eye));
        }
        for col in [0, 13] {
            cells[2][col] = Span::styled("▂", Style::new().fg(orange).bg(ear));
        }
        for cell in cells[4].iter_mut().take(13).skip(1) {
            *cell = Span::styled("▁", Style::new().fg(orange).bg(body));
        }
        // Four attached feet, each 1.5 columns wide by one row high. Mirrored
        // half-columns avoid the thin one-column stilts of the previous icon.
        for col in [2, 5, 8, 11] {
            cells[5][col] = Span::styled(" ", Style::new().bg(foot));
        }
        for col in [3, 6] {
            cells[5][col] = Span::styled("▌", Style::new().fg(foot));
        }
        for col in [7, 10] {
            cells[5][col] = Span::styled("▐", Style::new().fg(foot));
        }
        Self(cells.into_iter().map(Line::from).collect())
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
            assert_eq!(Robot::cream(theme, true).0.len(), 6);
        }
    }
    #[test]
    fn compact_mascot_keeps_reference_eye_baseline_and_mirrored_feet() {
        for theme in ["light", "dark"] {
            let robot = Robot::from_event(&serde_json::json!({
                "design": "cream-block-v2", "theme": theme
            }));
            assert_eq!(robot.0.len(), 6);
            assert!(robot.0.iter().all(|row| row.width() == 14));
            // Two identical ivory squares, 2 columns x 1 row each, mirrored
            // about the screen's centre and surrounded by black.
            for col in [4, 5, 8, 9] {
                assert_eq!(
                    robot.0[2].spans[col].style.bg,
                    Some(Color::Rgb(255, 253, 245))
                );
                assert_eq!(robot.0[2].spans[col].content, " ");
            }
            for col in [3, 6, 7, 10] {
                assert_eq!(robot.0[2].spans[col].style.bg, Some(Color::Rgb(14, 14, 14)));
            }
            let feet = &robot.0[5].spans;
            for col in [2, 5, 8, 11] {
                assert_eq!(feet[col].style.bg, Some(Color::Rgb(207, 196, 183)));
            }
            assert_eq!(feet[3].content, "▌");
            assert_eq!(feet[6].content, "▌");
            assert_eq!(feet[7].content, "▐");
            assert_eq!(feet[10].content, "▐");
            // Gaps stay transparent on any terminal background.
            assert_eq!(feet[4].style, Style::default());
            assert_eq!(feet[9].style, Style::default());
            assert!(robot.0[4].spans[1..13].iter().all(|s| s.content == "▁"));
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
