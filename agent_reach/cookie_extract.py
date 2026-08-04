# -*- coding: utf-8 -*-
"""Извлечение куки из локальных браузеров с минимальными привилегиями.

Поддерживает: Chrome, Firefox, Edge, Brave, Opera
Извлекает куки для одной явно запрошенной платформы за раз.

Использование:
    agent-reach configure --from-browser chrome --platform xueqiu
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, TypedDict

from agent_reach.utils.text import scrub_url_credentials
from agent_reach.utils.url import domain_matches


class PlatformSpec(TypedDict):
    name: str
    domains: Tuple[str, ...]
    cookies: Optional[Tuple[str, ...]]
    config_key: str


class ChromiumPaths(TypedDict):
    darwin: str
    linux: str
    win32: Tuple[str, ...]


PLATFORM_SPECS: Tuple[PlatformSpec, ...] = (
    {
        "name": "Twitter/X",
        "domains": (".x.com", ".twitter.com"),
        "cookies": ("auth_token", "ct0"),
        "config_key": "twitter",
    },
    {
        "name": "XiaoHongShu",
        "domains": (".xiaohongshu.com",),
        "cookies": None,  # только ручной экспорт через Cookie-Editor
        "config_key": "xhs",
    },
    {
        "name": "Bilibili",
        "domains": (".bilibili.com",),
        "cookies": ("SESSDATA", "bili_jct"),
        "config_key": "bilibili",
    },
    {
        "name": "Xueqiu",
        "domains": (".xueqiu.com",),
        "cookies": ("xq_a_token",),
        "config_key": "xueqiu",
    },
)

_PLATFORM_SPECS_BY_KEY: Dict[str, PlatformSpec] = {
    spec["config_key"]: spec for spec in PLATFORM_SPECS
}
SUPPORTED_BROWSERS = ("chrome", "firefox", "edge", "brave", "opera")
PROFILE_SELECTABLE_BROWSERS = ("chrome", "edge", "brave")
_MAX_XFETCH_SESSION_BYTES = 64 * 1024
_COOKIE_EDITOR_ONLY = {
    "twitter": "twitter-cookies",
    "xhs": "xhs-cookies",
}

_CHROMIUM_USER_DATA_DIRS: Dict[str, ChromiumPaths] = {
    "chrome": {
        "darwin": "~/Library/Application Support/Google/Chrome",
        "linux": "~/.config/google-chrome",
        "win32": ("Google", "Chrome", "User Data"),
    },
    "edge": {
        "darwin": "~/Library/Application Support/Microsoft Edge",
        "linux": "~/.config/microsoft-edge",
        "win32": ("Microsoft", "Edge", "User Data"),
    },
    "brave": {
        "darwin": "~/Library/Application Support/BraveSoftware/Brave-Browser",
        "linux": "~/.config/BraveSoftware/Brave-Browser",
        "win32": ("BraveSoftware", "Brave-Browser", "User Data"),
    },
}


@dataclass(frozen=True)
class BrowserConfigResult:
    """Результат настройки одного браузера с несекретными целями записи."""

    platform: str
    success: bool
    message: str
    targets: Tuple[str, ...] = ()

    def __iter__(self) -> Iterator[object]:
        """Сохраняет исторический API распаковки из трёх значений."""
        yield self.platform
        yield self.success
        yield self.message


def _chromium_user_data_dir(browser: str) -> Optional[Path]:
    """Возвращает корневой каталог профиля браузера в текущей ОС."""
    import os
    import sys

    paths = _CHROMIUM_USER_DATA_DIRS.get(browser)
    if paths is None:
        return None
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if not local_appdata:
            return None
        return Path(local_appdata).joinpath(*paths["win32"])
    path = paths["darwin"] if sys.platform == "darwin" else paths["linux"]
    return Path(os.path.expanduser(path))


def list_browser_profiles(browser: str = "chrome") -> List[Dict[str, str]]:
    """Возвращает именованные профили Chromium, содержащие базу куки."""
    browser = browser.lower()
    root = _chromium_user_data_dir(browser)
    if root is None:
        return []
    root = Path(root)
    if not root.is_dir():
        return []

    profiles = []
    for profile_dir in root.iterdir():
        if not profile_dir.is_dir():
            continue
        cookie_file = profile_dir / "Network" / "Cookies"
        if not cookie_file.is_file():
            cookie_file = profile_dir / "Cookies"
        if cookie_file.is_file():
            profiles.append(
                {
                    "folder": profile_dir.name,
                    "cookies_path": str(cookie_file),
                }
            )

    def sort_key(item):
        folder = item["folder"]
        suffix = folder.rsplit(" ", 1)[-1]
        number = int(suffix) if suffix.isdigit() else 0
        return (folder != "Default", number, folder)

    profiles.sort(key=sort_key)
    return profiles


def _profile_cookie_file(browser: str, profile: str) -> str:
    """Находит явно указанный профиль или выбрасывает ошибку без отката к Default."""
    if browser not in PROFILE_SELECTABLE_BROWSERS:
        raise ValueError(
            "Выбор профиля поддерживается только для "
            f"{', '.join(PROFILE_SELECTABLE_BROWSERS)}, "
            f"а не для {scrub_url_credentials(browser)}."
        )

    profiles = list_browser_profiles(browser)
    for candidate in profiles:
        if candidate["folder"] == profile:
            return candidate["cookies_path"]

    available = ", ".join(
        scrub_url_credentials(item["folder"]) for item in profiles
    )
    hint = f" Доступные профили: {available}." if available else ""
    raise ValueError(
        f"Профиль '{scrub_url_credentials(profile)}' не найден для "
        f"{scrub_url_credentials(browser)}.{hint}"
    )


def _platform_spec(platform: Optional[str]) -> PlatformSpec:
    """Возвращает спецификацию одной явно запрошенной платформы."""
    if not platform:
        raise ValueError(
            "Для извлечения куки из браузера необходимо указать платформу; "
            f"выберите одну из: {', '.join(_PLATFORM_SPECS_BY_KEY)}"
        )
    key = platform.lower()
    try:
        return _PLATFORM_SPECS_BY_KEY[key]
    except KeyError as exc:
        raise ValueError(
            f"Неподдерживаемая платформа: {scrub_url_credentials(platform)}. "
            f"Поддерживаются: {', '.join(_PLATFORM_SPECS_BY_KEY)}"
        ) from exc


def _require_browser_extractable(spec: PlatformSpec) -> None:
    """Отклоняет платформы, чья политика требует ручного экспорта куки."""
    manual_key = _COOKIE_EDITOR_ONLY.get(spec["config_key"])
    if manual_key:
        raise ValueError(
            f"Автоматическое извлечение из браузера отключено для {spec['name']}. "
            "Экспортируйте нужные куки через Cookie-Editor, затем используйте "
            f"`agent-reach configure {manual_key}`."
        )


def extract_all(
    browser: str = "chrome",
    *,
    platform: Optional[str] = None,
    profile: Optional[str] = None,
) -> Dict[str, dict]:
    """
    Извлекает куки для одной явно запрошенной платформы.

    Старое имя функции сохранено для обратной совместимости API,
    но чтение всех платформ за один раз намеренно больше не поддерживается.

    Возвращает:
        {"xueqiu": {"xq_a_token": "xxx"}}
    """
    spec = _platform_spec(platform)
    _require_browser_extractable(spec)
    browser = browser.lower()
    if browser not in SUPPORTED_BROWSERS:
        raise ValueError(
            f"Неподдерживаемый браузер: {scrub_url_credentials(browser)}. "
            f"Поддерживаются: {', '.join(SUPPORTED_BROWSERS)}"
        )
    cookie_file = _profile_cookie_file(browser, profile) if profile else None
    needed_cookies = spec["cookies"]
    if needed_cookies is None:
        raise ValueError(
            f"Автоматическое извлечение по всему домену отключено для {spec['name']}."
        )

    # Сначала пробуем rookiepy (на Rust, стабильнее), затем browser_cookie3
    use_rookiepy = False
    if cookie_file is None:
        try:
            import rookiepy
            use_rookiepy = True
        except ImportError:
            pass
    if not use_rookiepy:
        try:
            import browser_cookie3
        except ImportError:
            profile_hint = (
                f" для профиля '{scrub_url_credentials(profile)}'"
                if profile is not None
                else ""
            )
            raise RuntimeError(
                f"Извлечение куки{profile_hint} требует browser_cookie3"
                " (или rookiepy, если профиль не выбран).\n"
                "Установите: pip install browser-cookie3"
            )

    if use_rookiepy:
        # rookiepy возвращает список словарей с ключами name/value/domain/path
        try:
            browser_funcs = {
                "chrome": rookiepy.chrome,
                "firefox": rookiepy.firefox,
                "edge": rookiepy.edge,
                "brave": rookiepy.brave,
                "opera": rookiepy.opera,
            }
            raw_cookies = browser_funcs[browser](list(spec["domains"]))
            # Оборачиваем в объекты с .name, .value, .domain для совместимости
            class _Cookie:
                def __init__(self, d):
                    self.name = d.get("name", "")
                    self.value = d.get("value", "")
                    self.domain = d.get("domain", "")
            cookie_jar = [_Cookie(c) for c in raw_cookies]
        except Exception as e:
            raise RuntimeError(
                f"Не удалось прочитать куки {browser} через rookiepy: "
                f"{scrub_url_credentials(e)}\n"
                f"Убедитесь, что {browser} закрыт и у вас есть права доступа."
            )
    else:
        browser_funcs = {
            "chrome": browser_cookie3.chrome,
            "firefox": browser_cookie3.firefox,
            "edge": browser_cookie3.edge,
            "brave": browser_cookie3.brave,
            "opera": browser_cookie3.opera,
        }
        try:
            cookie_jar = []
            seen = set()
            for domain in spec["domains"]:
                kwargs = {"domain_name": domain}
                if cookie_file is not None:
                    kwargs["cookie_file"] = cookie_file
                for cookie in browser_funcs[browser](**kwargs):
                    identity = (
                        getattr(cookie, "name", ""),
                        getattr(cookie, "domain", ""),
                        getattr(cookie, "path", ""),
                        getattr(cookie, "value", ""),
                    )
                    if identity not in seen:
                        seen.add(identity)
                        cookie_jar.append(cookie)
        except Exception as e:
            raise RuntimeError(
                f"Не удалось прочитать куки {browser}: {scrub_url_credentials(e)}\n"
                f"Убедитесь, что {browser} закрыт и у вас есть права доступа."
            )

    results = {}

    platform_cookies = {}
    for cookie in cookie_jar:
        # Перепроверяем возвращённые куки вместо слепого доверия фильтру бэкенда.
        if not domain_matches(cookie.domain, *spec["domains"]):
            continue

        if cookie.name in needed_cookies:
            platform_cookies[cookie.name] = cookie.value

    if platform_cookies:
        results[spec["config_key"]] = platform_cookies

    return results


def _read_xfetch_session(path: Path) -> dict:
    """Читает небольшой устаревший файл сессии без перехода по символическим ссылкам."""
    import json

    from agent_reach.utils.paths import read_small_text_no_follow

    payload = read_small_text_no_follow(
        path,
        max_bytes=_MAX_XFETCH_SESSION_BYTES,
    )
    if payload is None:
        return {}
    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise ValueError("Файл сессии xfetch должен быть JSON-объектом")
    return loaded


def _sync_xfetch_session(auth_token: str, ct0: str) -> bool:
    """Синхронизирует учётные данные Twitter в ~/.config/xfetch/session.json (совместимость с xreach)."""
    import json
    import os

    try:
        from agent_reach.utils.paths import (
            atomic_write_private_text,
            make_private_dir,
        )

        xfetch_dir = os.path.join(os.path.expanduser("~"), ".config", "xfetch")
        make_private_dir(xfetch_dir)
        session_path = Path(xfetch_dir) / "session.json"
        session_data = _read_xfetch_session(session_path)
        session_data["authToken"] = auth_token
        session_data["ct0"] = ct0
        atomic_write_private_text(
            session_path,
            json.dumps(session_data, indent=2),
        )
        return True
    except Exception:
        # Некритично: конфиг agent-reach — основной источник истины, синхронизация xfetch — по возможности
        return False


def _sync_bird_env(auth_token: str, ct0: str) -> bool:
    """Записывает учётные данные Twitter в ~/.config/bird/credentials.env для CLI bird.

    bird читает AUTH_TOKEN и CT0 из переменных окружения. Эта функция создаёт
    файл, который можно подключить через `source ~/.config/bird/credentials.env`.
    Значения проходят через shlex.quote, чтобы токен с кавычкой, $ или обратным
    апострофом не сломал синтаксис оболочки при подключении файла.
    """
    import os
    import shlex

    try:
        from agent_reach.utils.paths import (
            atomic_write_private_text,
            make_private_dir,
        )

        bird_dir = os.path.join(os.path.expanduser("~"), ".config", "bird")
        make_private_dir(bird_dir)
        env_path = os.path.join(bird_dir, "credentials.env")
        atomic_write_private_text(
            env_path,
            f"AUTH_TOKEN={shlex.quote(auth_token)}\n"
            f"CT0={shlex.quote(ct0)}\n",
        )
        return True
    except Exception:
        # Некритично: конфиг agent-reach — основной источник истины, синхронизация bird — по возможности
        return False


# Псевдоним для вызывающего кода, ожидающего имя _sync_bird_credentials
_sync_bird_credentials = _sync_bird_env


def configure_from_browser(
    browser: str,
    config,
    *,
    platform: Optional[str] = None,
    profile: Optional[str] = None,
) -> List[BrowserConfigResult]:
    """
    Извлекает и настраивает ровно одну явно выбранную платформу.

    Объекты-результаты по-прежнему распаковываются как ``(platform, success, message)``
    и предоставляют атрибут ``targets``, чтобы CLI мог показать все несекретные
    ключи конфигурации или записанные устаревшие пути.
    """
    spec = _platform_spec(platform)
    _require_browser_extractable(spec)
    results_list: List[BrowserConfigResult] = []

    try:
        extracted = extract_all(
            browser,
            platform=spec["config_key"],
            profile=profile,
        )
    except ValueError:
        raise
    except Exception as e:
        return [
            BrowserConfigResult(
                "Браузер", False, scrub_url_credentials(e)
            )
        ]

    config_key = spec["config_key"]
    if config_key not in extracted:
        return [
            BrowserConfigResult(
                spec["name"],
                False,
                f"Куки {spec['name']} не найдены в {browser}. "
                f"Убедитесь, что вы вошли в выбранную платформу.",
            )
        ]

    if config_key == "bilibili":
        bc = extracted["bilibili"]
        if "SESSDATA" in bc:
            config.set("bilibili_sessdata", bc["SESSDATA"])
            targets = ["bilibili_sessdata"]
            if "bili_jct" in bc:
                config.set("bilibili_csrf", bc["bili_jct"])
                targets.append("bilibili_csrf")
            results_list.append(
                BrowserConfigResult(
                    "Bilibili",
                    True,
                    "SESSDATA" + (" + bili_jct" if "bili_jct" in bc else ""),
                    tuple(targets),
                )
            )
        else:
            results_list.append(
                BrowserConfigResult(
                    "Bilibili",
                    False,
                    f"SESSDATA не найден. "
                    f"Убедитесь, что вы вошли в bilibili.com в {browser}.",
                )
            )

    elif config_key == "xueqiu":
        token = extracted["xueqiu"].get("xq_a_token", "")
        if token:
            cookie_str = f"xq_a_token={token}"
            config.set("xueqiu_cookie", cookie_str)
            results_list.append(
                BrowserConfigResult(
                    "Xueqiu",
                    True,
                    "xq_a_token",
                    ("xueqiu_cookie",),
                )
            )
        else:
            results_list.append(
                BrowserConfigResult(
                    "Xueqiu",
                    False,
                    f"xq_a_token не найден, сначала войдите в xueqiu.com через {browser}",
                )
            )

    return results_list
