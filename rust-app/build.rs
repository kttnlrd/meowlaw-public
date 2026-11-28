fn main() {
    println!("cargo:rerun-if-changed=assets/lawkitty.ico");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut res = winresource::WindowsResource::new();
        res.set_icon("assets/lawkitty.ico");
        res.set("ProductName", "LawKitty");
        res.set("FileDescription", "LawKitty");
        if let Err(e) = res.compile() {
            println!("cargo:warning=no icon embedded: {e}");
        }
    }
}
