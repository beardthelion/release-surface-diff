def create_order(req):
    order = parse(req)
    db.save(order)
    return {"id": order.id}
