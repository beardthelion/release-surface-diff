"""PathMatcher scope filtering and targets.toml validation (KTD11, U9)."""

import pytest

from release_surface_diff import config


def _write(tmp_path, text):
    cfg = tmp_path / "targets.toml"
    cfg.write_text(text)
    return cfg


class TestPathMatcher:
    def test_include_only_matching(self):
        m = config.PathMatcher(include=["src/**"], exclude=())
        assert m.matches("src/x.py")
        assert not m.matches("docs/x.py")
        assert not m.matches("tests/x.py")
        assert not m.matches("x.py")


class TestWatchConfigValidation:
    def test_single_target_table_rejected(self, tmp_path):
        cfg = _write(tmp_path, '[target]\nrepo = "x"\n')
        with pytest.raises(config.ConfigError):
            config.load_watch_config(cfg)

    def test_scalar_target_list_rejected(self, tmp_path):
        cfg = _write(tmp_path, 'target = ["x"]\n')
        with pytest.raises(config.ConfigError):
            config.load_watch_config(cfg)

    def test_duplicate_repo_rejected(self, tmp_path):
        cfg = _write(
            tmp_path,
            '[[target]]\nrepo = "x"\n\n[[target]]\nrepo = "x"\n',
        )
        with pytest.raises(config.ConfigError, match="duplicate"):
            config.load_watch_config(cfg)

    def test_include_must_be_string_list(self, tmp_path):
        cfg = _write(tmp_path, '[[target]]\nrepo = "x"\ninclude = "src/**"\n')
        with pytest.raises(config.ConfigError, match="include"):
            config.load_watch_config(cfg)

    def test_exclude_must_be_string_list(self, tmp_path):
        cfg = _write(tmp_path, '[[target]]\nrepo = "x"\nexclude = [1, 2]\n')
        with pytest.raises(config.ConfigError, match="exclude"):
            config.load_watch_config(cfg)
