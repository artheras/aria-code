//! Multi-line prompt editing. Cursor positions are UTF-8 boundaries; movement
//! and deletion use whole graphemes, and layout uses terminal display columns.
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

pub const MAX_INPUT: usize = 65_536;

#[derive(Debug, Default, Clone)]
pub struct Editor {
    text: String,
    cursor: usize,
    history: Vec<String>,
    browsing: Option<(usize, String)>,
}

impl Editor {
    pub fn text(&self) -> &str {
        &self.text
    }
    pub fn is_empty(&self) -> bool {
        self.text.is_empty()
    }
    pub fn set(&mut self, text: &str) {
        self.clear();
        self.insert(text);
    }
    pub fn clear(&mut self) {
        self.text.clear();
        self.cursor = 0;
        self.browsing = None;
    }
    pub fn take(&mut self) -> String {
        let text = self.text.clone();
        if !text.trim().is_empty() && self.history.last() != Some(&text) {
            self.history.push(text.clone());
            if self.history.len() > 500 {
                self.history.remove(0);
            }
        }
        self.clear();
        text
    }
    pub fn insert(&mut self, text: &str) {
        // Keep pasted CRLF/multi-line text; strip terminal and bidi controls.
        let clean: String = text
            .replace("\r\n", "\n")
            .chars()
            .filter_map(|c| match c {
                '\r' => Some('\n'),
                '\t' => Some(' '),
                c if (c.is_control() && c != '\n')
                    || matches!(c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}') =>
                {
                    None
                }
                c => Some(c),
            })
            .collect();
        let remaining = MAX_INPUT.saturating_sub(self.text.len());
        let mut n = 0;
        for g in clean.graphemes(true) {
            if n + g.len() > remaining {
                break;
            }
            n += g.len();
        }
        self.text.insert_str(self.cursor, &clean[..n]);
        self.cursor += n;
        // Combining characters can join a neighboring grapheme.
        while self.cursor < self.text.len()
            && !self
                .text
                .grapheme_indices(true)
                .any(|(i, _)| i == self.cursor)
        {
            self.cursor += self.text[self.cursor..].chars().next().unwrap().len_utf8();
        }
        self.browsing = None;
    }
    pub fn left(&mut self) {
        self.cursor = self.text[..self.cursor]
            .grapheme_indices(true)
            .next_back()
            .map_or(0, |(i, _)| i);
    }
    pub fn right(&mut self) {
        if let Some(g) = self.text[self.cursor..].graphemes(true).next() {
            self.cursor += g.len();
        }
    }
    pub fn backspace(&mut self) {
        let end = self.cursor;
        self.left();
        self.text.drain(self.cursor..end);
        self.browsing = None;
    }
    pub fn delete(&mut self) {
        if let Some(g) = self.text[self.cursor..].graphemes(true).next() {
            self.text.drain(self.cursor..self.cursor + g.len());
        }
        self.browsing = None;
    }
    fn line_start(&self) -> usize {
        self.text[..self.cursor].rfind('\n').map_or(0, |i| i + 1)
    }
    fn line_end(&self) -> usize {
        self.text[self.cursor..]
            .find('\n')
            .map_or(self.text.len(), |i| self.cursor + i)
    }
    pub fn home(&mut self) {
        self.cursor = self.line_start();
    }
    pub fn end(&mut self) {
        self.cursor = self.line_end();
    }
    pub fn kill_before(&mut self) {
        let start = self.line_start();
        self.text.drain(start..self.cursor);
        self.cursor = start;
    }
    pub fn kill_after(&mut self) {
        self.text.drain(self.cursor..self.line_end());
    }
    pub fn kill_word(&mut self) {
        let end = self.cursor;
        while self.cursor > 0
            && self.text[..self.cursor]
                .chars()
                .next_back()
                .is_some_and(char::is_whitespace)
        {
            self.left();
        }
        while self.cursor > 0
            && !self.text[..self.cursor]
                .chars()
                .next_back()
                .is_some_and(char::is_whitespace)
        {
            self.left();
        }
        self.text.drain(self.cursor..end);
    }
    pub fn up(&mut self) -> bool {
        let start = self.line_start();
        if start == 0 {
            return false;
        }
        let column = self.text[start..self.cursor].width();
        self.cursor = start - 1;
        self.move_column(column);
        true
    }
    pub fn down(&mut self) -> bool {
        let end = self.line_end();
        if end == self.text.len() {
            return false;
        }
        let column = self.text[self.line_start()..self.cursor].width();
        self.cursor = end + 1;
        self.move_column(column);
        true
    }
    fn move_column(&mut self, column: usize) {
        let start = self.line_start();
        let end = self.line_end();
        self.cursor = start;
        let mut x = 0;
        for g in self.text[start..end].graphemes(true) {
            if x + g.width() > column {
                break;
            }
            x += g.width();
            self.cursor += g.len();
        }
    }
    pub fn history_prev(&mut self) {
        let (index, draft) = match self.browsing.take() {
            Some((0, d)) => {
                self.browsing = Some((0, d));
                return;
            }
            Some((i, d)) => (i - 1, d),
            None if self.history.is_empty() => return,
            None => (self.history.len() - 1, self.text.clone()),
        };
        self.set(&self.history[index].clone());
        self.browsing = Some((index, draft));
    }
    pub fn history_next(&mut self) {
        let Some((index, draft)) = self.browsing.take() else {
            return;
        };
        if index + 1 >= self.history.len() {
            self.set(&draft);
        } else {
            self.set(&self.history[index + 1].clone());
            self.browsing = Some((index + 1, draft));
        }
    }
    pub fn layout(&self, width: u16) -> (Vec<String>, (u16, u16)) {
        self.layout_text(width, false)
    }
    pub fn layout_text(&self, width: u16, secret: bool) -> (Vec<String>, (u16, u16)) {
        let width = usize::from(width.max(2));
        let mut rows = vec![String::new()];
        let mut x = 0;
        let mut cursor = (0, 0);
        for (i, g) in self.text.grapheme_indices(true) {
            let glyph = if secret && g != "\n" { "*" } else { g };
            let w = glyph.width();
            if g != "\n" && x + w > width {
                rows.push(String::new());
                x = 0;
            }
            if i == self.cursor {
                cursor = (rows.len() - 1, x);
            }
            if g == "\n" {
                rows.push(String::new());
                x = 0;
            } else {
                rows.last_mut().unwrap().push_str(glyph);
                x += w;
            }
        }
        if self.cursor == self.text.len() {
            if x >= width {
                rows.push(String::new());
                x = 0;
            }
            cursor = (rows.len() - 1, x);
        }
        (
            rows,
            (cursor.0.min(u16::MAX as usize) as u16, cursor.1 as u16),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn graphemes_are_not_split() {
        let mut e = Editor::default();
        e.insert("你e\u{301}👩‍💻");
        e.backspace();
        assert_eq!(e.text(), "你e\u{301}");
        e.left();
        e.delete();
        assert_eq!(e.text(), "你");
    }
    #[test]
    fn history_restores_draft() {
        let mut e = Editor::default();
        e.insert("one");
        e.take();
        e.insert("two");
        e.take();
        e.insert("draft");
        e.history_prev();
        e.history_prev();
        e.history_prev();
        assert_eq!(e.text(), "one");
        e.history_next();
        e.history_next();
        assert_eq!(e.text(), "draft");
    }
    #[test]
    fn display_columns_and_wrapped_cursor() {
        let mut e = Editor::default();
        e.set("abc你好\nx");
        assert_eq!(
            e.layout(5),
            (vec!["abc你".into(), "好".into(), "x".into()], (2, 1))
        );
        e.set("abcd你");
        e.left();
        assert_eq!(e.layout(5).1, (1, 0));
        e.set("abcde");
        assert_eq!(e.layout(5).1, (1, 0));
        e.set("你好\nabc");
        e.home();
        e.right();
        e.up();
        assert_eq!(e.cursor, 0);
    }
    #[test]
    fn limits_bytes_and_strips_controls() {
        let mut e = Editor::default();
        e.insert(&"你".repeat(MAX_INPUT));
        assert!(e.text().len() <= MAX_INPUT);
        e.set("a\r\nb\t\u{1b}\u{202e}");
        assert_eq!(e.text(), "a\nb ");
        assert_eq!(e.layout_text(80, true).0, ["*", "**"]);
    }
}
