from pathlib import Path

from backend.game import Catalog, Room, Session


def make_catalog():
    return Catalog.load(Path())


def test_two_player_game():
    catalog = make_catalog()
    sessions = {}
    room = Room(
        code="TEST01",
        difficulty=catalog.difficulties[0],
        visibility="private",
        sessions_map=sessions,
        catalog=catalog,
    )

    def add(sid, name):
        s = Session(
            id=sid,
            player_name=name,
            difficulty=catalog.difficulties[0],
            room_code="TEST01",
        )
        sessions[sid] = s
        room.sessions.append(sid)

    add("s1", "Alice")
    add("s2", "Bob")

    game = room.start_game()
    assert game.number == 1
    assert game.turn_order == ["s1", "s2"]
    assert game.active_sid() == "s1"
    assert game.current_word is not None

    p1 = game.get_participant("s1")
    result, _rankings = game.submit_guess(p1, "WRONGANSWER")
    assert result.correct is False
    assert p1.eliminated is True
    assert game.active_sid() == "s2"

    p2 = game.get_participant("s2")
    correct_word = game.current_word["word"]
    result2, rankings2 = game.submit_guess(p2, correct_word)
    assert result2.correct is True
    assert rankings2 is not None
    assert game.winner == "Bob"
    assert game.last_match_results[0]["name"] == "Bob"
    assert game.last_match_results[0]["rank"] == 1
    assert game.last_match_results[1]["name"] == "Alice"
    assert game.last_match_results[1]["rank"] == 2


def test_forfeit():
    catalog = make_catalog()
    sessions = {}
    room = Room(
        code="TEST02",
        difficulty=catalog.difficulties[0],
        visibility="private",
        sessions_map=sessions,
        catalog=catalog,
    )

    for sid, name in [("s1", "Alice"), ("s2", "Bob")]:
        s = Session(
            id=sid,
            player_name=name,
            difficulty=catalog.difficulties[0],
            room_code="TEST02",
        )
        sessions[sid] = s
        room.sessions.append(sid)

    game = room.start_game()
    assert game.active_sid() == "s1"

    rankings = room.forfeit("s1")
    assert "s1" not in room.sessions
    assert game.winner == "Bob"
    assert rankings is not None
    assert rankings[1]["name"] == "Bob"
    assert rankings[1]["rank"] == 1


def test_spectator_excluded_from_game():
    catalog = make_catalog()
    sessions = {}
    room = Room(
        code="TEST03",
        difficulty=catalog.difficulties[0],
        visibility="public",
        sessions_map=sessions,
        catalog=catalog,
    )

    for sid, name in [("s1", "Alice"), ("s2", "Bob")]:
        s = Session(
            id=sid,
            player_name=name,
            difficulty=catalog.difficulties[0],
            room_code="TEST03",
        )
        sessions[sid] = s
        room.sessions.append(sid)

    s3 = Session(id="s3", player_name="Eve", difficulty=catalog.difficulties[0], room_code="TEST03")
    sessions["s3"] = s3
    room.sessions.append("s3")
    room.spectators.add("s3")

    game = room.start_game()
    assert "s3" not in game.turn_order
    assert "s3" not in game.participants
    assert room.player_status("s3") == ("Spectating", "spectating")


def test_loser_goes_first_on_rematch():
    catalog = make_catalog()
    sessions = {}
    room = Room(
        code="TEST04",
        difficulty=catalog.difficulties[0],
        visibility="public",
        sessions_map=sessions,
        catalog=catalog,
    )

    for sid, name in [("s1", "Alice"), ("s2", "Bob")]:
        s = Session(
            id=sid,
            player_name=name,
            difficulty=catalog.difficulties[0],
            room_code="TEST04",
        )
        sessions[sid] = s
        room.sessions.append(sid)

    game = room.start_game()
    p1 = game.get_participant("s1")
    game.submit_guess(p1, "WRONG")
    p2 = game.get_participant("s2")
    game.submit_guess(p2, game.current_word["word"])

    assert game.winner == "Bob"
    room.intermission_until = 0
    room.start_new_game()
    game2 = room.current_game
    assert game2.turn_order[0] == "s1"  # loser goes first
