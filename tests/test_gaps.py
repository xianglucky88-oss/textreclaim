import pytest

def test_encoding_is_open_gap():
    from src.textreclaim import encoding
    with pytest.raises(NotImplementedError):
        encoding.process({"id": "x"})

def test_extract_is_open_gap():
    from src.textreclaim import extract
    with pytest.raises(NotImplementedError):
        extract.process({"id": "x"})
