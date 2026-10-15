from qsft.recording import Recorder, read_recording


def test_round_trip(tmp_path):
    path = tmp_path / "r.jsonl.gz"
    rec = Recorder(path, "test", {"a": 1})
    rec.write("lh", u="raw", d={"LHR-1": [1, 200, 0.1]})
    rec.write("mark", t=42, text="hello")
    rec.close()
    records = list(read_recording(path))
    assert records[0]["k"] == "hdr" and records[0]["command"] == "test"
    assert records[1]["d"]["LHR-1"] == [1, 200, 0.1]
    assert records[2] == {"k": "mark", "t": 42, "text": "hello"}


def test_truncated_recording_is_still_readable(tmp_path):
    path = tmp_path / "r.jsonl.gz"
    rec = Recorder(path, "test", {})
    for i in range(1000):
        rec.write("x", i=i)
    rec.close()
    data = path.read_bytes()
    path.write_bytes(data[: len(data) * 2 // 3])
    records = list(read_recording(path))
    assert records[0]["k"] == "hdr"
    assert [r["i"] for r in records[1:]] == list(range(len(records) - 1))
