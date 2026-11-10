fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match aria_physical::entry(&args) {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    }
}
