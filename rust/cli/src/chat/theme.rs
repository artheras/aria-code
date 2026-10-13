use ratatui::style::Color;

/// Text colours only. The mascot has its own palette.
#[derive(Clone, Copy)]
pub struct Palette {
    pub accent: Color,
    pub muted: Color,
    pub assistant: Color,
    pub success: Color,
    pub error: Color,
    pub code_bg: Color,
    pub selection_fg: Color,
}

pub fn resolve(theme: &str) -> String {
    for candidate in [
        std::env::var("ARIA_THEME").unwrap_or_default(),
        theme.into(),
    ] {
        if candidate == "light" || candidate == "dark" {
            return candidate;
        }
    }
    if let Ok(colors) = std::env::var("COLORFGBG") {
        if let Some(background) = colors.rsplit(';').next().and_then(|v| v.parse::<u8>().ok()) {
            return if background >= 7 { "light" } else { "dark" }.into();
        }
    }
    #[cfg(target_os = "macos")]
    {
        if let Ok(output) = std::process::Command::new("/usr/bin/defaults")
            .args(["read", "-g", "AppleInterfaceStyle"])
            .stderr(std::process::Stdio::null())
            .output()
        {
            return if output.status.success()
                && String::from_utf8_lossy(&output.stdout).trim() == "Dark"
            {
                "dark"
            } else {
                "light"
            }
            .into();
        }
    }
    "dark".into()
}
impl Palette {
    pub fn for_theme(theme: &str) -> Self {
        if theme == "light" {
            Self {
                accent: Color::Rgb(153, 85, 22),
                muted: Color::Rgb(95, 95, 95),
                assistant: Color::Rgb(15, 112, 139),
                success: Color::Rgb(25, 112, 63),
                error: Color::Rgb(177, 38, 38),
                code_bg: Color::Rgb(242, 242, 242),
                selection_fg: Color::White,
            }
        } else {
            Self {
                accent: Color::Rgb(225, 164, 103),
                muted: Color::Rgb(160, 160, 160),
                assistant: Color::Rgb(91, 192, 216),
                success: Color::Rgb(125, 190, 120),
                error: Color::Rgb(240, 115, 115),
                code_bg: Color::Rgb(32, 32, 32),
                selection_fg: Color::Black,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn luminance(color: Color) -> f64 {
        let Color::Rgb(r, g, b) = color else {
            panic!("text palette must use explicit RGB colours")
        };
        let linear = |byte: u8| {
            let c = f64::from(byte) / 255.0;
            if c <= 0.04045 {
                c / 12.92
            } else {
                ((c + 0.055) / 1.055).powf(2.4)
            }
        };
        0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
    }

    #[test]
    fn text_is_readable_on_the_supported_light_and_dark_backgrounds() {
        for (theme, background) in [
            ("light", Color::Rgb(255, 255, 255)),
            ("dark", Color::Rgb(14, 14, 14)),
        ] {
            let p = Palette::for_theme(theme);
            for foreground in [p.accent, p.muted, p.assistant, p.success, p.error] {
                let (a, b) = (luminance(foreground), luminance(background));
                assert!(
                    (a.max(b) + 0.05) / (a.min(b) + 0.05) >= 4.5,
                    "{theme}: {foreground:?}"
                );
            }
        }
    }
}
