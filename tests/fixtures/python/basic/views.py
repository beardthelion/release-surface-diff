def detail(request, pk):
    return pk


def old(request, slug):
    return slug


class ClassView:
    @classmethod
    def as_view(cls):
        return cls()
