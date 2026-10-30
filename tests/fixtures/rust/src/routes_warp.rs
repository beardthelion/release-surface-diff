//! warp filter-composition routes (R9).

use warp::Filter;

async fn get_user(_id: u64) -> Result<impl warp::Reply, warp::Rejection> {
    Ok(warp::reply::reply())
}
async fn create_user() -> Result<impl warp::Reply, warp::Rejection> {
    Ok(warp::reply::reply())
}
async fn list_things() -> Result<impl warp::Reply, warp::Rejection> {
    Ok(warp::reply::reply())
}

pub fn routes() -> impl Filter<Extract = impl warp::Reply, Error = warp::Rejection> + Clone {
    let users = warp::path("users")
        .and(warp::path::param::<u64>())
        .and(warp::get())
        .map(get_user);
    let create = warp::path("users")
        .and(warp::post())
        .and(warp::body::json())
        .and_then(create_user);
    let nested = warp::path("api")
        .and(warp::path!("v1" / "things" / u64))
        .and(warp::get())
        .map(list_things);
    users.or(create).or(nested)
}
