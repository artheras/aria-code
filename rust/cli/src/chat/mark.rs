//! The mascot's mark: one cell of it, for everywhere the whole robot is too much.
//!
//! The robot is the product's face and belongs on the welcome screen, where a
//! picture earns its space. It does not belong on every screen after that, and
//! the chat has had no activity indicator at all — nothing says the model is
//! working, and nothing carries the brand once the banner scrolls away.
//!
//! Claude Code solves both with one character: a glyph that prefixes lines and
//! animates through a family of related shapes while it works, so the brand
//! mark and the spinner are the same thing. The approach is worth taking; the
//! glyph is not, because it is theirs.
//!
//! So the mark is taken from the robot instead. Its ears and base line are a
//! lower block in #F9B467, and the mark is that detail: the same colour, the same
//! vocabulary of partial blocks, breathing through heights while a turn runs.
//! It reads as the robot looking back, not as a borrowed sparkle.

use ratatui::{
    style::{Color, Style},
    text::Span,
};

/// The ears and stripe are this orange; it is what the robot is
/// recognised by once it is too small to have a face.
pub const ORANGE: Color = Color::Rgb(249, 180, 103);

/// Resting: the eye as the robot wears it.
pub const RESTING: &str = "▄";

/// Working: the same eye, opening and closing. Partial blocks only — the
/// robot's own vocabulary, so the mark never stops looking like it came from
/// the robot.
pub const BREATH: [&str; 6] = ["▁", "▂", "▄", "▆", "▄", "▂"];

/// The mark at rest, for prefixing a line that is not working.
pub fn resting() -> Span<'static> {
    Span::styled(RESTING, Style::new().fg(ORANGE))
}

/// The mark at `tick`, for a turn in progress. Wraps, so a caller may keep
/// counting without bounding the frame itself.
pub fn breathing(tick: usize) -> Span<'static> {
    Span::styled(BREATH[tick % BREATH.len()], Style::new().fg(ORANGE))
}

/// The mark followed by its space, the way a line prefix is written.
pub fn prefix(tick: Option<usize>) -> Vec<Span<'static>> {
    vec![
        match tick {
            Some(t) => breathing(t),
            None => resting(),
        },
        Span::raw(" "),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_mark_is_the_colour_the_robot_is_recognised_by() {
        // Changing this makes the mark stop being the robot's.
        assert_eq!(ORANGE, Color::Rgb(249, 180, 103));
        assert_eq!(resting().style.fg, Some(ORANGE));
        assert_eq!(breathing(0).style.fg, Some(ORANGE));
    }

    #[test]
    fn resting_is_the_eye_as_the_robot_wears_it() {
        assert_eq!(resting().content, RESTING);
    }

    #[test]
    fn breathing_cycles_and_never_panics_on_a_running_count() {
        // The caller counts ticks for the life of a turn; the frame has to
        // wrap rather than index past the end.
        for tick in [0usize, 5, 6, 7, 1_000_000] {
            let span = breathing(tick);
            assert!(BREATH.contains(&span.content.as_ref()));
        }
        assert_eq!(breathing(0).content, breathing(BREATH.len()).content);
    }

    #[test]
    fn every_frame_is_one_cell_wide() {
        // A frame of another width would make the text after it jump.
        for frame in BREATH {
            assert_eq!(frame.chars().count(), 1);
        }
        assert_eq!(RESTING.chars().count(), 1);
    }

    #[test]
    fn the_frames_are_the_robots_own_vocabulary() {
        // Partial blocks, as the robot is drawn. A sparkle here would be
        // borrowed from somewhere else.
        for frame in BREATH {
            assert!("▁▂▃▄▅▆▇█".contains(frame), "{frame} is not a partial block");
        }
    }

    #[test]
    fn the_prefix_leaves_a_space_before_the_text() {
        let spans = prefix(None);
        assert_eq!(spans.len(), 2);
        assert_eq!(spans[1].content, " ");
    }

    #[test]
    fn a_working_prefix_differs_from_a_resting_one() {
        assert_ne!(prefix(Some(0))[0].content, prefix(Some(3))[0].content);
    }
}
