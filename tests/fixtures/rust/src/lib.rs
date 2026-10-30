//! Fixture crate root. Syntactically valid for `syn`; it is never compiled.

pub mod api;
pub mod cfg;
pub mod flags;
pub mod proto;
pub mod rpc;
pub mod routes_actix;
pub mod routes_axum;
pub mod routes_warp;

pub use api::Engine as Node;

pub fn run() {
    api::main_entry();
}
