//! Render the canonical Python mascot's spans, with no Rust-specific artwork.
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
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn spans_keep_exact_glyphs_and_foreground_background() {
        let value = serde_json::json!([
            [{"style":"#F4EBE4 on #0E0E0E", "text":"▗▛▀▀▀▀▀▜▖"}],
            [{"style":"#F9B467 on #0E0E0E", "text":"▌▌▗▖ ▂ ▐▐"}],
            [{"style":"on #0E0E0E", "text":"▐▙▄▄▄▄▄▟▌"}],
            [{"style":"#B6ADA4 on #D5CCC3", "text":"▝▀▀▀▀▀▀▀▘"}]
        ]);
        let robot = Robot::from_event(&value);
        assert_eq!(robot.0.len(), 4);
        assert_eq!(robot.0[1].spans[0].content, "▌▌▗▖ ▂ ▐▐");
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
