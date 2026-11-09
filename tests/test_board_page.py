"""The read-only Board page is served by the static /ui mount."""


def test_board_page_and_assets_are_served(client):
    page = client.get("/ui/board.html")
    assert page.status_code == 200
    assert 'id="board"' in page.text
    assert 'src="board.js"' in page.text
    assert client.get("/ui/board.js").status_code == 200
    assert client.get("/ui/board.css").status_code == 200


def test_channels_page_links_to_board(client):
    assert 'href="/ui/board.html"' in client.get("/ui/").text
