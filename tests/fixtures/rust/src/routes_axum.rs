//! axum route registrations (R9).

use axum::{
    middleware,
    routing::{delete, get, post},
    Router,
};

pub fn app() -> Router {
    let inner = Router::new()
        .route("/users/:id", get(user_by_id))
        .route("/users", post(create_user));
    let extra = Router::new().route("/status", get(status));
    Router::new()
        .route("/health", get(health))
        .route("/items/{item_id}", delete(remove_item))
        .route("/orders/{*rest}", get(order_tail))
        .route_service("/static", file_service())
        .route(
            "/admin",
            get(admin_panel).route_layer(middleware::from_fn(require_auth)),
        )
        .nest("/api", inner)
        .merge(extra)
        .layer(middleware::from_fn(trace_requests))
}

async fn user_by_id() {}
async fn create_user() {}
async fn health() {}
async fn remove_item() {}
async fn order_tail() {}
async fn status() {}
async fn admin_panel() {}
fn file_service() -> tower_http::services::ServeDir {
    tower_http::services::ServeDir::new("public")
}
async fn require_auth() {}
async fn trace_requests() {}
