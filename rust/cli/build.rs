fn main() {
    let path = "../../src/aria_code/_version.py";
    println!("cargo:rerun-if-changed={path}");
    let source = std::fs::read_to_string(path).expect("Aria product version");
    let line = source
        .lines()
        .find(|s| s.starts_with("__version__ = "))
        .expect("version assignment");
    let version = line.split('"').nth(1).expect("quoted product version");
    println!("cargo:rustc-env=ARIA_PRODUCT_VERSION={version}");
}
