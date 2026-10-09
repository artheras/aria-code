from text import shout, slugify


def test_slugify():
    assert slugify("Hello Big World") == "hello-big-world"


def test_shout():
    assert shout("a") == "A"
