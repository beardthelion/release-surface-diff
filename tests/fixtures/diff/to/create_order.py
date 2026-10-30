def create_order(req):
    if len(req.items) > MAX_ITEMS:
        abort(400)
    order = parse(req)
    db.save(order)
    return {"id": order.id}
