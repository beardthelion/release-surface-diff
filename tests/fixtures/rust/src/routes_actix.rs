//! actix-web registrations (R9): attribute routes, resource/scope chains.

use actix_web::{get, post, web, App, HttpResponse};

#[get("/users/{id}")]
async fn user_by_id() -> HttpResponse {
    HttpResponse::Ok().finish()
}

#[post("/users")]
async fn create_user() -> HttpResponse {
    HttpResponse::Ok().finish()
}

#[route("/multi", method = "GET", method = "HEAD")]
async fn multi() -> HttpResponse {
    HttpResponse::Ok().finish()
}

async fn list_items() -> HttpResponse {
    HttpResponse::Ok().finish()
}
async fn create_item() -> HttpResponse {
    HttpResponse::Ok().finish()
}
async fn put_thing() -> HttpResponse {
    HttpResponse::Ok().finish()
}

pub fn app() -> App<actix_web::body::MessageBody> {
    App::new()
        .service(user_by_id)
        .service(
            web::resource("/items")
                .route(web::get().to(list_items))
                .route(web::post().to(create_item)),
        )
        .service(
            web::scope("/api").service(
                web::resource("/v1/things").route(web::put().to(put_thing)),
            ),
        )
        .wrap(Logger)
}

struct Logger;
