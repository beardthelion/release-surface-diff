def legacy():
    total = 0
    for row in rows():
        total += row.value
        if total > LIMIT:
            break
    log(total)
    return {"total": total}
