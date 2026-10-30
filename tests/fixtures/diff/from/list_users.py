def list_users(user_id):
    user = db.get(user_id)
    if user is None:
        abort(404)
    return user.to_json()
