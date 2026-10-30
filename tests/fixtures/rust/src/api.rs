//! Export surface: pub items reachable outside the crate; pub(crate) and
//! private-ancestor items are not entry points.

pub mod server {
    pub fn serve() {}
    pub(crate) fn internal_serve() {}

    mod hidden {
        pub fn secret() {}
    }
}

pub(crate) mod internals {
    pub fn helper() {}
}

pub fn main_entry() {
    helper_call();
    server::serve();
}

fn helper_call() {}

pub(crate) fn crate_only() {}
fn private_fn() {}

pub struct PublicConfig {
    pub field: u32,
}

pub enum Mode {
    A,
    B,
}

pub struct Engine;
impl Engine {
    pub fn start() {}
    fn stop(&self) {}
}

pub trait Runner {
    fn run(&self);
}
