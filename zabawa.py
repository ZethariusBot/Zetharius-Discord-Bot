import asyncio
import copy
import json
import os
import re
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp
import discord
import yt_dlp
from dotenv import load_dotenv
from discord import app_commands
from discord.ext import commands, tasks


# Wczytaj sekrety z pliku .env obok tego skryptu (hosting nie udostępnia pól env).
load_dotenv()


# Wstaw tutaj ID serwera Discord, na którym mają pojawić się komendy.
GUILD_ID = 1428016568801169430
BOT_OWNER_ID = 1357986195585503284
CONTENT_PING_ROLE_IDS = (
    1428090513034313869,
    1428090515156631663,
    1428090517131886672,
)
LEVELS_FILE = "levels.json"
XP_MIN = 15
XP_MAX = 25
XP_COOLDOWN_SECONDS = 60
BOT_STARTED_AT = int(datetime.now(timezone.utc).timestamp())

# ---------- Konfiguracja właściciela i uprawnień ----------

def czy_wlasciciel(user):
    return getattr(user, "id", None) == BOT_OWNER_ID


def wlasciciel_lub_uprawnienie(nazwa_uprawnienia):
    async def sprawdz(interaction: discord.Interaction):
        if czy_wlasciciel(interaction.user):
            return True
        uprawnienia = getattr(interaction.user, "guild_permissions", None)
        return bool(getattr(uprawnienia, nazwa_uprawnienia, False))

    return app_commands.check(sprawdz)


def wlasciciel_lub_organizator(user, content):
    return (
        czy_wlasciciel(user)
        or user.id == content["organizator_id"]
        or getattr(getattr(user, "guild_permissions", None), "manage_guild", False)
    )


def wymagane_xp(poziom):
    """XP potrzebne do przejścia z danego poziomu na następny."""
    return 100 + poziom * 50


def odczytaj_poziomy():
    try:
        with open(LEVELS_FILE, "r", encoding="utf-8") as plik:
            dane = json.load(plik)
            return dane if isinstance(dane, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def zapisz_poziomy(dane):
    plik_tymczasowy = f"{LEVELS_FILE}.tmp"
    with open(plik_tymczasowy, "w", encoding="utf-8") as plik:
        json.dump(dane, plik, ensure_ascii=False, indent=2)
    os.replace(plik_tymczasowy, LEVELS_FILE)


poziomy = odczytaj_poziomy()
ostatnie_xp = {}
blokada_poziomow = asyncio.Lock()


# ---------- Bot i komendy slash ----------

class SlashBot(commands.Bot):
    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        print("[START] Synchronizuję komendy Discorda...")
        await self.tree.sync(guild=guild)
        print("[START] Komendy zsynchronizowane.")
        for content_id, content in albion_contenty.items():
            if content.get("message_id") and not content.get("odwolany") and not content.get("karta_usunieta"):
                self.add_view(ContentView(content_id), message_id=content["message_id"])
        for guild_id, panel in albion_panele.items():
            if panel.get("message_id"):
                serwer_id = int(panel.get("guild_id") or guild_id.split(":", 1)[0])
                self.add_view(FinanceLauncherView(serwer_id), message_id=panel["message_id"])
        self.loop.create_task(odswiez_komponenty_po_starcie())
        self.loop.create_task(sprzataj_zakonczone_contenty())
        self.loop.create_task(aktualizuj_panel_statusu_albion())
        self.loop.create_task(aktualizuj_killlog_bellatores())


async def odswiez_komponenty_po_starcie():
    await bot.wait_until_ready()
    # Sprzątanie zakończonych contentów działa równolegle i może usuwać wpisy,
    # gdy odświeżanie czeka na odpowiedź Discorda. Iterujemy po migawce słownika.
    for content_id, content in list(albion_contenty.items()):
        if not content.get("message_id") or content.get("odwolany") or content.get("karta_usunieta"):
            continue
        try:
            channel = bot.get_channel(int(content["channel_id"])) or await bot.fetch_channel(int(content["channel_id"]))
            message = await channel.fetch_message(int(content["message_id"]))
            await message.edit(embed=zbuduj_embed_contentu(content), view=ContentView(content_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, KeyError, TypeError, ValueError) as error:
            print(f"Nie udało się odświeżyć karty contentu {content_id}: {error}")
    for guild_id, panel in list(albion_panele.items()):
        if not panel.get("message_id"):
            continue
        try:
            serwer_id = int(panel.get("guild_id") or guild_id.split(":", 1)[0])
            channel = bot.get_channel(int(panel["channel_id"])) or await bot.fetch_channel(int(panel["channel_id"]))
            message = await channel.fetch_message(int(panel["message_id"]))
            await message.edit(view=FinanceLauncherView(serwer_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, KeyError, TypeError, ValueError) as error:
            print(f"Nie udało się odświeżyć panelu rozliczeń {guild_id}: {error}")
    print("[START] Odświeżanie starych kart i paneli zakończone.")


intents = discord.Intents.default()
intents.message_content = True

bot = SlashBot(
    command_prefix=commands.when_mentioned,
    intents=intents,
)

# Rotujące aktywności Rich Presence. Komenda /moderacja status może je
# tymczasowo zastąpić; wpisanie "auto" przywraca rotację.
PRESENCE_ROTATION_SECONDS = 45
presence_rotation_index = -1
custom_presence = None


def odmiana_polskiego_liczebnika(liczba, jeden, kilka, wiele):
    liczba = abs(int(liczba))
    if liczba == 1:
        return jeden
    if 2 <= liczba % 10 <= 4 and not 12 <= liczba % 100 <= 14:
        return kilka
    return wiele


def zbuduj_aktywnosci_presence():
    aktywne_contenty = [
        content for content in albion_contenty.values()
        if not content.get("zakonczony_at")
        and not content.get("odwolany")
        and not content.get("karta_usunieta")
    ]
    zapisani = {
        str(gracz_id)
        for content in aktywne_contenty
        for gracz_id in content.get("uczestnicy", [])
    }
    statusy_znane = [
        str(dane.get("type", "unknown")).lower()
        for dane in ostatnie_statusy_serwerow_albion.values()
        if isinstance(dane, dict)
    ]
    online = sum(status == "online" for status in statusy_znane)
    liczba_contentow = len(aktywne_contenty)
    opis_contentow = odmiana_polskiego_liczebnika(
        liczba_contentow,
        "aktywny content",
        "aktywne contenty",
        "aktywnych contentów",
    )
    liczba_graczy = len(zapisani)
    opis_graczy = odmiana_polskiego_liczebnika(
        liczba_graczy,
        "gracz zapisany",
        "gracze zapisani",
        "graczy zapisanych",
    )
    liczba_serwerow = len(bot.guilds)
    opis_serwerow = odmiana_polskiego_liczebnika(
        liczba_serwerow,
        "serwerze Discord",
        "serwerach Discord",
        "serwerach Discord",
    )
    if any(status != "unknown" for status in statusy_znane):
        opis_swiatow = f"🌍 {online}/{len(ALBION_STATUS_SERVERS)} światów online"
    else:
        opis_swiatow = "🌍 pobieram status światów"

    aktywnosci = [
        discord.Activity(
            type=discord.ActivityType.playing,
            name="⚔️ Albion Online | Bellatores",
            details="Przygoda trwa",
            state="Zetharius • Albion tools",
        ),
    ]

    if aktywne_contenty:
        content = aktywne_contenty[0]
        zapisani_contentu = len(set(str(uid) for uid in content.get("uczestnicy", [])))
        limit_miejsc = max(1, int(content.get("limit", 1) or 1))
        aktywnosci.append(
            discord.Activity(
                type=discord.ActivityType.playing,
                name=f"⚔️ {str(content.get('nazwa') or 'Content Bellatores')[:100]}",
                details=f"📅 {str(content.get('termin') or 'termin do ustalenia')[:100]}",
                state=f"👥 {zapisani_contentu}/{limit_miejsc} miejsc • {str(content.get('typ') or 'Albion')[:40]}",
                party={
                    "id": f"zetharius-content-{content.get('id', 'active')}",
                    "size": [min(zapisani_contentu, limit_miejsc), limit_miejsc],
                },
            )
        )

    aktywnosci.extend([
        discord.Activity(
            type=discord.ActivityType.watching,
            name=f"📋 {liczba_contentow} {opis_contentow}",
            details=f"👥 {liczba_graczy} unikalnych zapisów",
            state="CONTENTY • BELLATORES",
        ),
        discord.Activity(
            type=discord.ActivityType.listening,
            name=f"👥 {liczba_graczy} {opis_graczy}",
            details=f"📅 {liczba_contentow} aktywnych wydarzeń",
            state="ZAPISY GILDII",
        ),
        discord.Activity(
            type=discord.ActivityType.watching,
            name=opis_swiatow,
            details="Ameryka • Azja • Europa",
            state="STATUS SERWERÓW ALBION",
        ),
        discord.Activity(
            type=discord.ActivityType.listening,
            name="📊 /albion • rynek i profile",
            details="Ceny, gracze i gildie",
            state="NARZĘDZIA ALBION",
        ),
        discord.Activity(
            type=discord.ActivityType.watching,
            name="🎖️ profile World of Tanks",
            details="Gracze • czołgi • klany",
            state="NOWA SEKCJA /WOT • PC EU",
        ),
        discord.Activity(
            type=discord.ActivityType.watching,
            name="☠️ killboard • Bellatores",
            details="Kille • deady • lokalizacje",
            state="ALBION ONLINE • LIVE",
        ),
        discord.Activity(
            type=discord.ActivityType.playing,
            name=f"🛡️ na {liczba_serwerow} {opis_serwerow}",
            details="Gildia • contenty • narzędzia",
            state="ZETHARIUS BOT",
        ),
    ])
    for aktywnosc in aktywnosci:
        aktywnosc.timestamps = {"start": BOT_STARTED_AT}
    return tuple(aktywnosci)


async def ustaw_kolejny_status():
    global presence_rotation_index
    aktywnosci = zbuduj_aktywnosci_presence()
    presence_rotation_index = (presence_rotation_index + 1) % len(aktywnosci)
    await bot.change_presence(
        status=discord.Status.dnd,
        activity=aktywnosci[presence_rotation_index],
    )


@tasks.loop(seconds=PRESENCE_ROTATION_SECONDS)
async def rotuj_rich_presence():
    if custom_presence is not None:
        return
    await ustaw_kolejny_status()


@rotuj_rich_presence.before_loop
async def przed_rotacja_rich_presence():
    await bot.wait_until_ready()

# ---------- Grupy komend Discord ----------

moderacja = app_commands.Group(name="moderacja", description="Narzędzia administracyjne i moderacyjne.")
muzyka = app_commands.Group(name="muzyka", description="Sterowanie odtwarzaczem muzyki.")
albion = app_commands.Group(name="albion", description="Informacje o graczach, gildiach i rynku Albion Online.")
wot = app_commands.Group(name="wot", description="Profile, czołgi i klany World of Tanks PC EU.")
ogolne = app_commands.Group(name="ogolne", description="Podstawowe komendy bota i system poziomów.")
narzedzia = app_commands.Group(name="narzedzia", description="Pozostałe narzędzia bota.")
bot.tree.add_command(moderacja)
bot.tree.add_command(muzyka)
bot.tree.add_command(albion)
bot.tree.add_command(wot)
bot.tree.add_command(ogolne)
bot.tree.add_command(narzedzia)


@bot.event
async def on_ready():
    print(f"Bot online jako {bot.user}")
    if not rotuj_rich_presence.is_running():
        rotuj_rich_presence.start()


@bot.tree.error
async def obsluz_blad_komendy(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        wiadomosc = "Nie masz uprawnień do użycia tej komendy."
    elif isinstance(error, app_commands.BotMissingPermissions):
        wiadomosc = "Bot nie ma wymaganych uprawnień na serwerze."
    else:
        print(f"Błąd komendy slash: {error}")
        wiadomosc = "Wystąpił błąd podczas wykonywania komendy."

    if interaction.response.is_done():
        await interaction.followup.send(wiadomosc, ephemeral=True)
    else:
        await interaction.response.send_message(wiadomosc, ephemeral=True)


# ---------- Komendy podstawowe: ping, poziomy i ranking ----------

@ogolne.command(name="ping", description="Sprawdź, czy bot odpowiada: Pong!")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")


@bot.event
async def on_message(wiadomosc: discord.Message):
    if wiadomosc.guild is None or wiadomosc.author.bot:
        return

    if len(wiadomosc.content.strip()) < 3:
        return

    klucz = f"{wiadomosc.guild.id}:{wiadomosc.author.id}"
    teraz = datetime.now(timezone.utc)

    async with blokada_poziomow:
        ostatni_czas = ostatnie_xp.get(klucz)
        if ostatni_czas and (teraz - ostatni_czas).total_seconds() < XP_COOLDOWN_SECONDS:
            return

        ostatnie_xp[klucz] = teraz
        dane = poziomy.setdefault(klucz, {"xp": 0, "level": 0})
        poprzedni_poziom = int(dane.get("level", 0))
        dane["xp"] = int(dane.get("xp", 0)) + random.randint(XP_MIN, XP_MAX)

        while dane["xp"] >= wymagane_xp(int(dane.get("level", 0))):
            dane["xp"] -= wymagane_xp(int(dane.get("level", 0)))
            dane["level"] = int(dane.get("level", 0)) + 1

        zapisz_poziomy(poziomy)
        nowy_poziom = int(dane.get("level", 0))

    if nowy_poziom > poprzedni_poziom:
        await wiadomosc.channel.send(
            f"🎉 {wiadomosc.author.mention}, awansujesz na poziom **{nowy_poziom}**!"
        )


@ogolne.command(name="poziom", description="Pokazuje Twój poziom i postęp XP.")
@app_commands.guild_only()
@app_commands.describe(osoba="Wybierz gracza; puste pole pokaże Twój poziom.")
async def poziom(interaction: discord.Interaction, osoba: discord.Member | None = None):
    czlonek = osoba or interaction.user
    klucz = f"{interaction.guild_id}:{czlonek.id}"
    dane = poziomy.get(klucz, {"xp": 0, "level": 0})
    level = int(dane.get("level", 0))
    xp = int(dane.get("xp", 0))
    cel = wymagane_xp(level)
    await interaction.response.send_message(
        f"**{czlonek.display_name}** — poziom **{level}**\n"
        f"Postęp: **{xp}/{cel} XP**"
    )


@ogolne.command(name="ranking", description="Pokazuje 10 osób z największą liczbą poziomów na serwerze.")
@app_commands.guild_only()
async def ranking(interaction: discord.Interaction):
    prefiks = f"{interaction.guild_id}:"
    wyniki = []
    for klucz, dane in poziomy.items():
        if not klucz.startswith(prefiks):
            continue
        try:
            user_id = int(klucz.split(":", 1)[1])
            level = int(dane.get("level", 0))
            xp = int(dane.get("xp", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        wyniki.append((level, xp, user_id))

    wyniki.sort(reverse=True)
    if not wyniki:
        await interaction.response.send_message("Ranking jest jeszcze pusty.")
        return

    linie = []
    for miejsce, (level, xp, user_id) in enumerate(wyniki[:10], start=1):
        czlonek = interaction.guild.get_member(user_id)
        nazwa = czlonek.display_name if czlonek else f"Użytkownik {user_id}"
        linie.append(f"**{miejsce}.** {nazwa} — poziom **{level}** ({xp} XP)")

    await interaction.response.send_message("🏆 **Ranking poziomów**\n" + "\n".join(linie))


@moderacja.command(name="status", description="Ustaw Rich Presence albo przywróć automatyczną rotację.")
@wlasciciel_lub_uprawnienie("manage_guild")
@app_commands.describe(
    tekst="Tekst aktywności; wpisz auto, aby wrócić do rotacji.",
    typ="Sposób wyświetlania aktywności.",
)
@app_commands.choices(typ=[
    app_commands.Choice(name="Gra w", value="playing"),
    app_commands.Choice(name="Ogląda", value="watching"),
    app_commands.Choice(name="Słucha", value="listening"),
    app_commands.Choice(name="Rywalizuje w", value="competing"),
])
async def status(
    interaction: discord.Interaction,
    tekst: str,
    typ: app_commands.Choice[str] = None,
):
    global custom_presence, presence_rotation_index
    if tekst.strip().casefold() == "auto":
        custom_presence = None
        presence_rotation_index = -1
        await ustaw_kolejny_status()
        await interaction.response.send_message("Przywrócono automatyczną rotację Rich Presence.")
        return

    typ_aktywnosci = {
        "playing": discord.ActivityType.playing,
        "watching": discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
    }
    wybor = typ.value if typ else "playing"
    custom_presence = discord.Activity(type=typ_aktywnosci[wybor], name=tekst[:128])
    await bot.change_presence(status=discord.Status.dnd, activity=custom_presence)
    await interaction.response.send_message(
        f"Ustawiono aktywność: **{tekst[:128]}**. Wpisz `auto`, aby wrócić do rotacji."
    )


@narzedzia.command(
    name="pinguj",
    description="Wyślij od 1 do 100 wiadomości prywatnych. Używaj rozważnie.",
)
@app_commands.describe(osoba="Odbiorca wiadomości prywatnych.", liczba="Liczba wiadomości: od 1 do 100.")
async def pinguj(
    interaction: discord.Interaction,
    osoba: discord.User,
    liczba: app_commands.Range[int, 1, 100],
):
    await interaction.response.defer(ephemeral=True)

    try:
        for _ in range(liczba):
            await osoba.send(
                f"Ping! Wiadomość wysłana na prośbę {interaction.user}."
            )

        await interaction.followup.send(
            f"Wysłano {liczba} wiadomości do {osoba}.",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "Nie mogę wysłać tej osobie wiadomości prywatnej.",
            ephemeral=True,
        )
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"Discord nie przyjął wiadomości: {error}",
            ephemeral=True,
        )


# ---------- Grupa /moderacja ----------

@moderacja.command(name="ban", description="Banuje użytkownika na serwerze.")
@app_commands.guild_only()
@app_commands.describe(osoba="Wybierz osobę do zbanowania.", powod="Powód widoczny w logu moderacji.")
@wlasciciel_lub_uprawnienie("ban_members")
@app_commands.checks.bot_has_permissions(ban_members=True)
async def ban(
    interaction: discord.Interaction,
    osoba: discord.Member,
    powod: str = "Nie podano powodu",
):
    if osoba.id == interaction.user.id:
        await interaction.response.send_message(
            "Nie możesz zbanować siebie.",
            ephemeral=True,
        )
        return

    await osoba.ban(
        reason=f"{powod} | Moderator: {interaction.user}"
    )
    await interaction.response.send_message(
        f"Zbanowano **{osoba}**. Powód: {powod}"
    )


@moderacja.command(name="kick", description="Wyrzuca użytkownika z serwera.")
@app_commands.guild_only()
@app_commands.describe(osoba="Wybierz osobę do wyrzucenia.", powod="Powód widoczny w logu moderacji.")
@wlasciciel_lub_uprawnienie("kick_members")
@app_commands.checks.bot_has_permissions(kick_members=True)
async def kick(
    interaction: discord.Interaction,
    osoba: discord.Member,
    powod: str = "Nie podano powodu",
):
    if osoba.id == interaction.user.id:
        await interaction.response.send_message(
            "Nie możesz wyrzucić siebie.",
            ephemeral=True,
        )
        return

    await osoba.kick(
        reason=f"{powod} | Moderator: {interaction.user}"
    )
    await interaction.response.send_message(
        f"Wyrzucono **{osoba}**. Powód: {powod}"
    )


@moderacja.command(name="timeout", description="Nakłada timeout na użytkownika.")
@app_commands.guild_only()
@app_commands.describe(osoba="Wybierz osobę.", minuty="Czas w minutach: od 1 do 40320 (28 dni).", powod="Powód widoczny w logu moderacji.")
@wlasciciel_lub_uprawnienie("moderate_members")
@app_commands.checks.bot_has_permissions(moderate_members=True)
async def timeout_member(
    interaction: discord.Interaction,
    osoba: discord.Member,
    minuty: app_commands.Range[int, 1, 40320],
    powod: str = "Nie podano powodu",
):
    if osoba.id == interaction.user.id:
        await interaction.response.send_message(
            "Nie możesz nałożyć timeoutu na siebie.",
            ephemeral=True,
        )
        return

    await osoba.timeout(
        timedelta(minutes=minuty),
        reason=f"{powod} | Moderator: {interaction.user}",
    )
    await interaction.response.send_message(
        f"Timeout dla **{osoba}** na {minuty} min. Powód: {powod}"
    )


@moderacja.command(name="untimeout", description="Zdejmuje timeout użytkownika.")
@app_commands.guild_only()
@app_commands.describe(osoba="Wybierz osobę, której chcesz zdjąć timeout.")
@wlasciciel_lub_uprawnienie("moderate_members")
@app_commands.checks.bot_has_permissions(moderate_members=True)
async def untimeout(
    interaction: discord.Interaction,
    osoba: discord.Member,
):
    await osoba.timeout(
        None,
        reason=f"Zdjęcie timeoutu przez {interaction.user}",
    )
    await interaction.response.send_message(
        f"Zdjęto timeout z **{osoba}**."
    )


@moderacja.command(name="clear", description="Usuwa wiadomości z kanału.")
@app_commands.guild_only()
@app_commands.describe(liczba="Ile ostatnich wiadomości usunąć? Maksymalnie 100.")
@wlasciciel_lub_uprawnienie("manage_messages")
@app_commands.checks.bot_has_permissions(manage_messages=True)
async def clear(
    interaction: discord.Interaction,
    liczba: app_commands.Range[int, 1, 100],
):
    kanal = interaction.channel

    if kanal is None or not hasattr(kanal, "purge"):
        await interaction.response.send_message(
            "Nie mogę usuwać wiadomości w tym kanale.",
            ephemeral=True,
        )
        return

    usuniete = await kanal.purge(limit=liczba)
    await interaction.response.send_message(
        f"Usunięto {len(usuniete)} wiadomości.",
        ephemeral=True,
    )


@moderacja.command(name="unban", description="Odbanowuje użytkownika po ID.")
@app_commands.guild_only()
@app_commands.describe(user_id="Wklej numeryczne Discord ID zbanowanej osoby.", powod="Powód zapisany w logu moderacji.")
@wlasciciel_lub_uprawnienie("ban_members")
@app_commands.checks.bot_has_permissions(ban_members=True)
async def unban(
    interaction: discord.Interaction,
    user_id: str,
    powod: str = "Nie podano powodu",
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Tej komendy można użyć tylko na serwerze.",
            ephemeral=True,
        )
        return

    try:
        id_uzytkownika = int(user_id)
    except ValueError:
        await interaction.response.send_message(
            "ID użytkownika musi być liczbą.",
            ephemeral=True,
        )
        return

    try:
        uzytkownik = await bot.fetch_user(id_uzytkownika)
        await interaction.guild.unban(
            uzytkownik,
            reason=f"{powod} | Moderator: {interaction.user}",
        )
        await interaction.response.send_message(
            f"Odbanowano **{uzytkownik}**. Powód: {powod}"
        )
    except discord.NotFound:
        await interaction.response.send_message(
            "Nie znaleziono bana dla tego użytkownika.",
            ephemeral=True,
        )


# ---------- Grupa /muzyka ----------

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    ),
    "options": "-vn",
}

muzyka_kolejki = {}
muzyka_blokady = {}
muzyka_teraz_gra = {}
muzyka_loop = {}
muzyka_pomin = set()


def pobierz_audio(query: str):
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        info = ydl.extract_info(query, download=False)

    if "entries" in info:
        info = next((entry for entry in info["entries"] if entry), None)

    if not info:
        raise ValueError("Nie znaleziono utworu.")

    return info["url"], info.get("title", "Nieznany utwór")


async def obsluz_koniec_utworu(guild_id, kanal, query, error):
    if error:
        print(f"Błąd odtwarzania muzyki: {error}")

    pominieto = guild_id in muzyka_pomin
    muzyka_pomin.discard(guild_id)

    if not error and not pominieto and muzyka_loop.get(guild_id, False):
        muzyka_kolejki.setdefault(guild_id, []).insert(0, query)

    await odtworz_nastepny(guild_id, kanal)


async def odtworz_nastepny(guild_id, kanal):
    kolejka = muzyka_kolejki.setdefault(guild_id, [])
    blokada = muzyka_blokady.setdefault(guild_id, asyncio.Lock())

    async with blokada:
        guild = bot.get_guild(guild_id)
        voice = guild.voice_client if guild else None

        if voice is None or not voice.is_connected():
            return

        if voice.is_playing() or voice.is_paused():
            return

        if not kolejka:
            muzyka_teraz_gra.pop(guild_id, None)
            return

        query = kolejka.pop(0)

        try:
            stream_url, title = await asyncio.to_thread(
                pobierz_audio,
                query,
            )
            source = discord.FFmpegPCMAudio(
                stream_url,
                **FFMPEG_OPTIONS,
            )
            loop = asyncio.get_running_loop()

            def po_zakonczeniu(error):
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        obsluz_koniec_utworu(
                            guild_id,
                            kanal,
                            query,
                            error,
                        )
                    )
                )

            muzyka_teraz_gra[guild_id] = title
            voice.play(source, after=po_zakonczeniu)
            await kanal.send(f"Teraz odtwarzam: **{title}**")

        except Exception as error:
            await kanal.send(f"Nie udało się odtworzyć utworu: {error}")
            loop = asyncio.get_running_loop()
            loop.call_soon(
                lambda: asyncio.create_task(
                    odtworz_nastepny(guild_id, kanal)
                )
            )


@muzyka.command(name="play", description="Podaj tytuł lub link; bot dołączy do Twojego kanału głosowego.")
@app_commands.guild_only()
@app_commands.describe(utwor="Tytuł piosenki albo pełny link do utworu.")
async def music_play(
    interaction: discord.Interaction,
    utwor: str,
):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message(
            "Tej komendy można użyć tylko na serwerze.",
            ephemeral=True,
        )
        return

    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "Nie udało się pobrać danych członka serwera.",
            ephemeral=True,
        )
        return

    if member.voice is None or member.voice.channel is None:
        await interaction.response.send_message(
            "Najpierw dołącz do kanału głosowego.",
            ephemeral=True,
        )
        return

    voice = interaction.guild.voice_client

    try:
        if voice is None:
            voice = await member.voice.channel.connect()
        elif voice.channel != member.voice.channel:
            await interaction.response.send_message(
                "Bot jest już na innym kanale głosowym.",
                ephemeral=True,
            )
            return

        kolejka = muzyka_kolejki.setdefault(interaction.guild.id, [])
        kolejka.append(utwor)

        await interaction.response.send_message(
            f"Dodano do kolejki (**miejsce {len(kolejka)}**): {utwor}"
        )

        if not voice.is_playing() and not voice.is_paused():
            await odtworz_nastepny(
                interaction.guild.id,
                interaction.channel,
            )

    except Exception as error:
        wiadomosc = f"Nie udało się uruchomić muzyki: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(wiadomosc, ephemeral=True)
        else:
            await interaction.response.send_message(
                wiadomosc,
                ephemeral=True,
            )


@muzyka.command(name="queue", description="Pokazuje utwór odtwarzany teraz i następne pozycje kolejki.")
@app_commands.guild_only()
async def music_queue(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Tej komendy można użyć tylko na serwerze.",
            ephemeral=True,
        )
        return

    kolejka = muzyka_kolejki.get(interaction.guild.id, [])
    teraz = muzyka_teraz_gra.get(interaction.guild.id)

    if not teraz and not kolejka:
        await interaction.response.send_message("Kolejka jest pusta.")
        return

    linie = []
    if teraz:
        linie.append(f"**Teraz gra:** {teraz}")

    if kolejka:
        linie.append("**Następne utwory:**")
        linie.extend(
            f"{numer}. {utwor}"
            for numer, utwor in enumerate(kolejka[:10], start=1)
        )
        if len(kolejka) > 10:
            linie.append(f"…i jeszcze {len(kolejka) - 10} utworów.")

    await interaction.response.send_message("\n".join(linie))


@muzyka.command(name="loop", description="Włącza lub wyłącza powtarzanie aktualnego utworu.")
@app_commands.guild_only()
@app_commands.describe(wlacz="Wybierz tak/nie; pomiń, aby przełączyć obecny stan.")
async def music_loop(
    interaction: discord.Interaction,
    wlacz: bool | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Tej komendy można użyć tylko na serwerze.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild.id

    if wlacz is None:
        muzyka_loop[guild_id] = not muzyka_loop.get(guild_id, False)
    else:
        muzyka_loop[guild_id] = wlacz

    stan = "włączone" if muzyka_loop[guild_id] else "wyłączone"
    await interaction.response.send_message(
        f"Zapętlanie utworu: **{stan}**."
    )


@muzyka.command(name="skip", description="Pomija utwór odtwarzany teraz.")
@app_commands.guild_only()
async def music_skip(interaction: discord.Interaction):
    voice = interaction.guild.voice_client if interaction.guild else None

    if voice and (voice.is_playing() or voice.is_paused()):
        muzyka_pomin.add(interaction.guild.id)
        voice.stop()
        await interaction.response.send_message("Pominięto utwór.")
    else:
        await interaction.response.send_message(
            "Nic teraz nie jest odtwarzane.",
            ephemeral=True,
        )


@muzyka.command(name="pause", description="Wstrzymuje aktualnie odtwarzaną muzykę.")
@app_commands.guild_only()
async def music_pause(interaction: discord.Interaction):
    voice = interaction.guild.voice_client if interaction.guild else None

    if voice and voice.is_playing():
        voice.pause()
        await interaction.response.send_message("Wstrzymano muzykę.")
    else:
        await interaction.response.send_message(
            "Nic teraz nie gra.",
            ephemeral=True,
        )


@muzyka.command(name="resume", description="Wznawia wstrzymany utwór.")
@app_commands.guild_only()
async def music_resume(interaction: discord.Interaction):
    voice = interaction.guild.voice_client if interaction.guild else None

    if voice and voice.is_paused():
        voice.resume()
        await interaction.response.send_message("Wznowiono muzykę.")
    else:
        await interaction.response.send_message(
            "Muzyka nie jest wstrzymana.",
            ephemeral=True,
        )


@muzyka.command(name="stop", description="Zatrzymuje utwór i usuwa wszystkie pozycje z kolejki.")
@app_commands.guild_only()
async def music_stop(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Tej komendy można użyć tylko na serwerze.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild.id
    muzyka_kolejki[guild_id] = []
    muzyka_loop[guild_id] = False
    muzyka_teraz_gra.pop(guild_id, None)

    voice = interaction.guild.voice_client
    if voice and (voice.is_playing() or voice.is_paused()):
        voice.stop()

    await interaction.response.send_message(
        "Zatrzymano muzykę i wyczyszczono kolejkę."
    )


@muzyka.command(name="leave", description="Rozłącza bota i czyści kolejkę muzyczną.")
@app_commands.guild_only()
async def music_leave(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Tej komendy można użyć tylko na serwerze.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild.id
    muzyka_kolejki[guild_id] = []
    muzyka_loop[guild_id] = False
    muzyka_teraz_gra.pop(guild_id, None)

    voice = interaction.guild.voice_client
    if voice:
        if voice.is_playing() or voice.is_paused():
            voice.stop()
        await voice.disconnect()
        await interaction.response.send_message(
            "Bot opuścił kanał głosowy."
        )
    else:
        await interaction.response.send_message(
            "Bot nie jest na kanale głosowym.",
            ephemeral=True,
        )


# ---------- Albion Online: dane i konfiguracja ----------

ALBION_API = "https://gameinfo-ams.albiononline.com/api/gameinfo"
WOT_API_ROOT = "https://api.worldoftanks.eu/wot"
WOT_APPLICATION_ID = os.getenv("WOT_APPLICATION_ID", "").strip()
ALBION_MARKET_APIS = {
    "europe": "https://europe.albion-online-data.com/api/v2",
    "west": "https://west.albion-online-data.com/api/v2",
    "east": "https://east.albion-online-data.com/api/v2",
}
ALBION_HISTORY_FILE = "albion_guild_history.json"
ALBION_CONTENTS_FILE = "albion_contents.json"
ALBION_ACCOUNTS_FILE = "albion_accounts.json"
ALBION_PAYOUT_ROLE_ID = 1428090511528300554

# ---------- Albion Online: automatyczny status serwerów ----------

ALBION_STATUS_CHANNEL_ID = 1429185490287988959
ALBION_STATUS_FILE = "albion_server_status.json"
ALBION_STATUS_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AlbionOnline-Tank.webp")
ALBION_STATUS_PAGE_URL = "https://www.albionstatus.com/"
ALBION_STATUS_REFRESH_SECONDS = 60
ALBION_STATUS_SERVERS = {
    "was": "Americas (West)",
    "sgp": "Asia (East)",
    "ams": "Europe",
}
ALBION_STATUS_PAGE_LABELS = {
    "was": "Albion Main Server status",
    "sgp": "Albion Asia Server status",
    "ams": "Albion Europe Server status",
}
ostatnie_statusy_serwerow_albion = {}

# ---------- Albion Online: killlog i deathlog gildii Bellatores ----------

ALBION_KILLLOG_CHANNEL_ID = 1543930985161162804
ALBION_KILLLOG_GUILD_NAME = "Bellatores"
ALBION_KILLLOG_STATE_FILE = "albion_killlog_state.json"
ALBION_KILLLOG_REFRESH_SECONDS = 60
ALBION_KILLLOG_SERVERS = {
    "Americas (West)": "https://gameinfo.albiononline.com/api/gameinfo/events",
    "Asia (East)": "https://gameinfo-sgp.albiononline.com/api/gameinfo/events",
    "Europe": "https://gameinfo-ams.albiononline.com/api/gameinfo/events",
}


def odczytaj_stan_killloga():
    try:
        with open(ALBION_KILLLOG_STATE_FILE, "r", encoding="utf-8") as plik:
            dane = json.load(plik)
            return dane if isinstance(dane, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def zapisz_stan_killloga(stan):
    plik_tymczasowy = f"{ALBION_KILLLOG_STATE_FILE}.tmp"
    with open(plik_tymczasowy, "w", encoding="utf-8") as plik:
        json.dump(stan, plik, ensure_ascii=False, indent=2)
    os.replace(plik_tymczasowy, ALBION_KILLLOG_STATE_FILE)


def normalizuj_nazwe_albion(nazwa):
    return str(nazwa or "").strip().casefold()


def zdarzenie_dotyczy_bellatores(zdarzenie):
    szukana_nazwa = normalizuj_nazwe_albion(ALBION_KILLLOG_GUILD_NAME)
    for strona in (zdarzenie.get("Killer"), zdarzenie.get("Victim")):
        if not isinstance(strona, dict):
            continue
        for pole in ("GuildName", "AllianceName"):
            nazwa = normalizuj_nazwe_albion(strona.get(pole))
            if szukana_nazwa and szukana_nazwa in nazwa:
                return True
    return False


def opis_gracza_zdarzenia(gracz):
    if not isinstance(gracz, dict):
        return "Nieznany gracz"
    nazwa = str(gracz.get("Name") or "Nieznany gracz")
    gildia = str(gracz.get("GuildName") or "Brak gildii")
    sojusz = str(gracz.get("AllianceName") or "")
    opis = f"**{nazwa[:100]}**\nGildia: {gildia[:100]}"
    if sojusz:
        opis += f"\nSojusz: {sojusz[:100]}"
    return opis


def zbuduj_embed_zdarzenia_albion(zdarzenie, nazwa_serwera):
    zabojca = zdarzenie.get("Killer") or {}
    ofiara = zdarzenie.get("Victim") or {}
    gildia_zabojcy = normalizuj_nazwe_albion(zabojca.get("GuildName"))
    sojusz_zabojcy = normalizuj_nazwe_albion(zabojca.get("AllianceName"))
    zabojstwo_bellatores = (
        ALBION_KILLLOG_GUILD_NAME.casefold() in gildia_zabojcy
        or ALBION_KILLLOG_GUILD_NAME.casefold() in sojusz_zabojcy
    )
    typ = "⚔️ KILLLOG" if zabojstwo_bellatores else "💀 DEATHLOG"
    kolor = discord.Color.green() if zabojstwo_bellatores else discord.Color.red()
    event_id = zdarzenie.get("EventId")
    embed = discord.Embed(
        title=f"{typ} • Bellatores",
        color=kolor,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Zabójca", value=opis_gracza_zdarzenia(zabojca), inline=True)
    embed.add_field(name="Ofiara", value=opis_gracza_zdarzenia(ofiara), inline=True)

    lokacja = (
        zdarzenie.get("Location")
        or zdarzenie.get("LocationName")
        or zdarzenie.get("ClusterName")
        or zdarzenie.get("KillArea")
    )
    if isinstance(lokacja, dict):
        lokacja = lokacja.get("Name") or lokacja.get("name") or lokacja.get("Id")
    if not lokacja:
        lokacja = "API nie podało lokalizacji"
    embed.add_field(name="📍 Lokalizacja", value=str(lokacja)[:250], inline=False)

    fame = zdarzenie.get("TotalVictimKillFame")
    if fame is not None:
        embed.add_field(name="Sława za zabójstwo", value=formatuj_fame(fame), inline=True)
    embed.set_footer(text=f"Serwer: {nazwa_serwera} • Zdarzenie: {event_id or 'brak ID'}")
    return embed


async def pobierz_zdarzenia_serwera(sesja, url):
    try:
        async with sesja.get(url, params={"limit": 51, "offset": 0}) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            dane = await response.json()
            if not isinstance(dane, list):
                raise ValueError("API zwróciło nieoczekiwany format zdarzeń")
            return dane
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as error:
        print(f"Albion killlog: błąd pobierania {url}: {error!r}")
        return None


async def aktualizuj_killlog_bellatores():
    await bot.wait_until_ready()
    stan = odczytaj_stan_killloga()
    timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_read=25)

    while not bot.is_closed():
        kanal = bot.get_channel(ALBION_KILLLOG_CHANNEL_ID)
        try:
            if kanal is None:
                kanal = await bot.fetch_channel(ALBION_KILLLOG_CHANNEL_ID)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"Accept": "application/json", "User-Agent": "ZethariusAlbionBot/1.0"},
            ) as sesja:
                wyniki = await asyncio.gather(*(
                    pobierz_zdarzenia_serwera(sesja, url)
                    for url in ALBION_KILLLOG_SERVERS.values()
                ))

            for (nazwa_serwera, _), zdarzenia in zip(ALBION_KILLLOG_SERVERS.items(), wyniki):
                if zdarzenia is None:
                    continue
                znane = set(stan.get(nazwa_serwera, []))
                identyfikatory = [
                    str(zdarzenie.get("EventId"))
                    for zdarzenie in zdarzenia
                    if isinstance(zdarzenie, dict) and zdarzenie.get("EventId") is not None
                ]

                if nazwa_serwera not in stan:
                    # Pierwszy odczyt ustawia punkt startowy bez publikowania starych killów.
                    stan[nazwa_serwera] = identyfikatory[:300]
                    zapisz_stan_killloga(stan)
                    print(f"Albion killlog: ustawiono punkt startowy dla {nazwa_serwera}.")
                    continue

                nowe = [
                    zdarzenie for zdarzenie in zdarzenia
                    if isinstance(zdarzenie, dict)
                    and zdarzenie.get("EventId") is not None
                    and str(zdarzenie["EventId"]) not in znane
                ]
                nowe.sort(key=lambda zdarzenie: str(zdarzenie.get("TimeStamp") or ""))
                nieopublikowane = set()
                for zdarzenie in nowe:
                    event_id = str(zdarzenie["EventId"])
                    if not zdarzenie_dotyczy_bellatores(zdarzenie):
                        continue
                    try:
                        await kanal.send(embed=zbuduj_embed_zdarzenia_albion(zdarzenie, nazwa_serwera))
                    except (discord.Forbidden, discord.HTTPException) as error:
                        print(f"Albion killlog: nie udało się wysłać zdarzenia {event_id}: {error}")
                        nieopublikowane.add(event_id)

                aktualne = [event_id for event_id in identyfikatory if event_id not in nieopublikowane]
                stan[nazwa_serwera] = list(dict.fromkeys(aktualne + stan.get(nazwa_serwera, [])))[:300]
                zapisz_stan_killloga(stan)
        except (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
            print(f"Albion killlog: błąd aktualizacji: {error}")

        await asyncio.sleep(ALBION_KILLLOG_REFRESH_SECONDS)


def odczytaj_panel_statusu_albion():
    try:
        with open(ALBION_STATUS_FILE, "r", encoding="utf-8") as plik:
            dane = json.load(plik)
            return dane if isinstance(dane, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def zapisz_panel_statusu_albion(dane):
    plik_tymczasowy = f"{ALBION_STATUS_FILE}.tmp"
    with open(plik_tymczasowy, "w", encoding="utf-8") as plik:
        json.dump(dane, plik, ensure_ascii=False, indent=2)
    os.replace(plik_tymczasowy, ALBION_STATUS_FILE)


async def pobierz_statusy_serwerow_albion(sesja):
    try:
        async with sesja.get(ALBION_STATUS_PAGE_URL) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            strona = await response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as error:
        print(f"Status Albion: nie udało się pobrać strony trackera: {error!r}")
        return {
            region: {"type": "unknown", "message": "Nie udało się pobrać aktualnych danych."}
            for region in ALBION_STATUS_SERVERS
        }

    statusy = {}
    for region, etykieta in ALBION_STATUS_PAGE_LABELS.items():
        wzorzec = rf"<dt\b[^>]*>\s*{re.escape(etykieta)}\s*</dt>\s*<dd\b[^>]*>\s*([^<]+?)\s*</dd>"
        dopasowanie = re.search(wzorzec, strona, flags=re.IGNORECASE | re.DOTALL)
        if dopasowanie:
            statusy[region] = {"type": dopasowanie.group(1).strip().lower()}
        else:
            print(f"Status Albion {region}: strona nie zawiera pola '{etykieta}'.")
            statusy[region] = {"type": "unknown", "message": "Nie znaleziono statusu na stronie trackera."}
    return statusy


def zbuduj_embed_statusu_albion(statusy):
    embed = discord.Embed(
        title="🌍 Albion Online — status serwerów",
        description="Bieżąca dostępność wszystkich światów Albion Online.",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    kolory_statusu = {
        "online": ("🟢", "Online"),
        "offline": ("🔴", "Offline"),
        "maintenance": ("🟠", "Przerwa techniczna"),
        "restarting": ("🟠", "Restart"),
    }
    for region, nazwa in ALBION_STATUS_SERVERS.items():
        dane = statusy.get(region, {})
        typ = str(dane.get("type", "unknown")).lower()
        emoji, etykieta = kolory_statusu.get(typ, ("⚪", "Brak danych"))
        wartosc = f"**{emoji} {etykieta}**"
        komunikat = str(dane.get("message") or "").strip()
        if komunikat:
            wartosc += f"\n{komunikat[:250]}"
        embed.add_field(name=nazwa, value=wartosc, inline=True)
    embed.set_footer(text="Źródło: AlbionStatus • odświeżanie co minutę")
    return embed


async def aktualizuj_panel_statusu_albion():
    global ostatnie_statusy_serwerow_albion
    await bot.wait_until_ready()
    panel = odczytaj_panel_statusu_albion()
    timeout = aiohttp.ClientTimeout(total=15)

    while not bot.is_closed():
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sesja:
                statusy = await pobierz_statusy_serwerow_albion(sesja)
            ostatnie_statusy_serwerow_albion = statusy
            embed = zbuduj_embed_statusu_albion(statusy)

            kanal = bot.get_channel(ALBION_STATUS_CHANNEL_ID)
            if kanal is None:
                kanal = await bot.fetch_channel(ALBION_STATUS_CHANNEL_ID)

            wiadomosc = None
            if panel.get("channel_id") == ALBION_STATUS_CHANNEL_ID and panel.get("message_id"):
                try:
                    wiadomosc = await kanal.fetch_message(int(panel["message_id"]))
                except discord.NotFound:
                    wiadomosc = None

            if wiadomosc is None:
                if os.path.isfile(ALBION_STATUS_IMAGE):
                    embed.set_image(url="attachment://AlbionOnline-Tank.webp")
                    plik = discord.File(ALBION_STATUS_IMAGE, filename="AlbionOnline-Tank.webp")
                    wiadomosc = await kanal.send(embed=embed, file=plik)
                else:
                    print(f"Brak grafiki statusu Albion: {ALBION_STATUS_IMAGE}")
                    wiadomosc = await kanal.send(embed=embed)
                panel = {"channel_id": ALBION_STATUS_CHANNEL_ID, "message_id": wiadomosc.id}
                zapisz_panel_statusu_albion(panel)
            else:
                if wiadomosc.attachments:
                    embed.set_image(url=wiadomosc.attachments[0].url)
                    await wiadomosc.edit(embed=embed)
                elif os.path.isfile(ALBION_STATUS_IMAGE):
                    embed.set_image(url="attachment://AlbionOnline-Tank.webp")
                    plik = discord.File(ALBION_STATUS_IMAGE, filename="AlbionOnline-Tank.webp")
                    await wiadomosc.edit(embed=embed, attachments=[plik])
                else:
                    await wiadomosc.edit(embed=embed)
        except (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
            print(f"Nie udało się odświeżyć panelu statusu Albion: {error}")

        await asyncio.sleep(ALBION_STATUS_REFRESH_SECONDS)


def odczytaj_contenty():
    try:
        with open(ALBION_CONTENTS_FILE, "r", encoding="utf-8") as plik:
            dane = json.load(plik)
            return dane if isinstance(dane, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def zapisz_contenty():
    plik_tymczasowy = f"{ALBION_CONTENTS_FILE}.tmp"
    with open(plik_tymczasowy, "w", encoding="utf-8") as plik:
        json.dump(albion_contenty, plik, ensure_ascii=False, indent=2)
    os.replace(plik_tymczasowy, ALBION_CONTENTS_FILE)


albion_contenty = odczytaj_contenty()
zmiana_contentow = asyncio.Event()


def odczytaj_konta():
    try:
        with open(ALBION_ACCOUNTS_FILE, "r", encoding="utf-8") as plik:
            dane = json.load(plik)
            if isinstance(dane, dict):
                return dane.get("konta", {}), dane.get("panele", {}), dane.get("szablony_rol", {})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}, {}, {}


albion_konta, albion_panele, albion_szablony_rol = odczytaj_konta()


def zapisz_konta():
    plik_tymczasowy = f"{ALBION_ACCOUNTS_FILE}.tmp"
    with open(plik_tymczasowy, "w", encoding="utf-8") as plik:
        json.dump({"konta": albion_konta, "panele": albion_panele, "szablony_rol": albion_szablony_rol}, plik, ensure_ascii=False, indent=2)
    os.replace(plik_tymczasowy, ALBION_ACCOUNTS_FILE)


def klucz_konta(guild_id, user_id):
    return f"{guild_id}:{user_id}"


def saldo_konta(guild_id, user_id):
    konto = albion_konta.get(klucz_konta(guild_id, user_id), {})
    return (
        int(konto.get("zarobione", 0))
        - int(konto.get("wyplacone", 0))
        - int(konto.get("korekty_reset", 0))
    )


def moze_wyplacac(member):
    return (
        czy_wlasciciel(member)
        or member.guild_permissions.administrator
        or any(role.id == ALBION_PAYOUT_ROLE_ID for role in member.roles)
    )


async def sprawdz_rozliczajacego(interaction: discord.Interaction):
    return interaction.guild is not None and moze_wyplacac(interaction.user)


def zbuduj_embed_sald(guild_id):
    prefix = f"{guild_id}:"
    wiersze = []
    for klucz, konto in albion_konta.items():
        if not klucz.startswith(prefix):
            continue
        try:
            user_id = int(klucz.split(":", 1)[1])
            zarobione = int(konto.get("zarobione", 0))
            wyplacone = int(konto.get("wyplacone", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        pozostalo = saldo_konta(guild_id, user_id)
        wiersze.append((pozostalo, user_id, zarobione, wyplacone))

    wiersze.sort(reverse=True)
    embed = discord.Embed(title="💳 Panel rozliczeń Albion", color=discord.Color.gold())
    embed.description = "Wybierz gracza z listy, aby zarejestrować przekazanie mu srebra. Saldo zmieni się po potwierdzeniu wypłaty."
    if not wiersze:
        embed.add_field(name="Salda", value="Brak zaksięgowanych rozliczeń.", inline=False)
    else:
        linie = []
        for pozostalo, user_id, zarobione, wyplacone in wiersze[:10]:
            linie.append(f"<@{user_id}> — dostępne **{pozostalo:,}** | zarobione {zarobione:,} | przekazane {wyplacone:,}".replace(",", " "))
        if len(wiersze) > 10:
            linie.append(f"…i {len(wiersze) - 10} kolejnych graczy")
        embed.add_field(name="Salda graczy (srebro)", value="\n".join(linie), inline=False)
    embed.set_footer(text="Dostęp: administrator lub rola 1428090511528300554")
    return embed


class WplataGraczModal(discord.ui.Modal, title="Zarejestruj wypłatę"):
    kwota = discord.ui.TextInput(label="Kwota srebra", max_length=30)
    notatka = discord.ui.TextInput(label="Notatka / potwierdzenie", required=False, max_length=200)

    def __init__(self, guild_id, user_id):
        super().__init__(custom_id=f"albion:payout-modal:{guild_id}:{user_id}")
        self.guild_id = guild_id
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.guild.id != self.guild_id or not moze_wyplacac(interaction.user):
            await interaction.response.send_message("Wypłatę może zarejestrować administrator lub wskazana rola.", ephemeral=True)
            return
        try:
            kwota = int(str(self.kwota.value).replace(" ", ""))
            if kwota <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Podaj dodatnią kwotę srebra jako liczbę całkowitą.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        gracz = interaction.guild.get_member(self.user_id)
        if gracz is None:
            try:
                gracz = await interaction.guild.fetch_member(self.user_id)
            except discord.NotFound:
                await interaction.followup.send("Ten gracz nie należy do serwera.", ephemeral=True)
                return
            except discord.HTTPException:
                await interaction.followup.send("Nie udało się pobrać gracza z serwera.", ephemeral=True)
                return

        klucz = klucz_konta(interaction.guild.id, self.user_id)
        konto = albion_konta.setdefault(klucz, {"zarobione": 0, "wyplacone": 0, "transakcje": []})
        konto.setdefault("transakcje", [])
        pozostalo = saldo_konta(interaction.guild.id, self.user_id)
        if kwota > pozostalo:
            await interaction.followup.send(f"Saldo gracza to tylko **{pozostalo:,}** srebra.".replace(",", " "), ephemeral=True)
            return

        konto["wyplacone"] = int(konto.get("wyplacone", 0)) + kwota
        konto["transakcje"].append({
            "typ": "wyplata", "kwota": kwota, "czas": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "admin_id": interaction.user.id, "notatka": str(self.notatka.value),
        })
        try:
            zapisz_konta()
        except OSError as error:
            konto["wyplacone"] -= kwota
            konto["transakcje"].pop()
            print(f"Nie udało się zapisać wypłaty: {error}")
            await interaction.followup.send("Nie udało się zapisać wypłaty. Nie przekazuj silvera, dopóki panel nie potwierdzi operacji.", ephemeral=True)
            return
        nowe_saldo = pozostalo - kwota
        await interaction.followup.send(
            f"Zarejestrowano **{kwota:,}** srebra jako przekazane graczowi {gracz.mention}. "
            f"Pozostałe saldo: **{nowe_saldo:,}**.".replace(",", " "),
            ephemeral=True,
        )


class PayoutPanelView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.selected_user_id = None
        self.add_item(PayoutMemberSelect(self))

    async def interaction_check(self, interaction):
        if interaction.guild is None or interaction.guild.id != self.guild_id or not moze_wyplacac(interaction.user):
            await interaction.response.send_message("Dostęp ma administrator lub rola 1428090511528300554.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Odśwież salda", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh(self, interaction, button):
        await interaction.response.edit_message(embed=zbuduj_embed_sald(self.guild_id), view=self)

    @discord.ui.button(label="Zarejestruj wypłatę", style=discord.ButtonStyle.success, emoji="💸", row=1)
    async def payout(self, interaction, button):
        if self.selected_user_id is None:
            await interaction.response.send_message("Najpierw wybierz gracza z listy.", ephemeral=True)
            return
        await interaction.response.send_modal(WplataGraczModal(self.guild_id, self.selected_user_id))


class PayoutMemberSelect(discord.ui.UserSelect):
    def __init__(self, payout_view):
        super().__init__(
            placeholder="Wybierz gracza, któremu przekazujesz silver",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.payout_view = payout_view

    async def callback(self, interaction: discord.Interaction):
        self.payout_view.selected_user_id = self.values[0].id
        await interaction.response.defer(ephemeral=True)


def oczekujace_rozliczenia(guild_id):
    oczekujace = []
    for content_id, content in albion_contenty.items():
        try:
            pasuje_serwer = int(content.get("guild_id", 0)) == guild_id
        except (TypeError, ValueError, AttributeError):
            continue
        if pasuje_serwer and (content.get("rozliczenie") or {}).get("status") == "oczekuje":
            oczekujace.append((content_id, content))
    return oczekujace


def archiwalne_rozliczenia_gildii(guild_id):
    archiwum = []
    for content_id, content in albion_contenty.items():
        try:
            pasuje_serwer = int(content.get("guild_id", 0)) == guild_id
        except (TypeError, ValueError, AttributeError):
            continue
        rozliczenie = content.get("rozliczenie") or {}
        if (
            pasuje_serwer
            and content.get("rozliczenie_gildii")
            and content.get("zakonczony_at")
            and content.get("karta_usunieta")
            and not rozliczenie.get("zaksięgowane")
        ):
            archiwum.append((content_id, content))
    return archiwum


def podziel_silver_miedzy_uczestnikow(uczestnicy, kwota):
    odbiorcy = sorted(set(uczestnicy))
    na_osobe, reszta = divmod(kwota, len(odbiorcy)) if odbiorcy else (0, 0)
    return {
        str(user_id): na_osobe + (1 if index < reszta else 0)
        for index, user_id in enumerate(odbiorcy)
    }


class PendingSettlementSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        if parent_view.tryb_gildia:
            oczekujace = archiwalne_rozliczenia_gildii(parent_view.guild_id)[:25]
        else:
            oczekujace = oczekujace_rozliczenia(parent_view.guild_id)[:25]
        opcje = [
            discord.SelectOption(
                label=str(content.get("nazwa", "Content"))[:100],
                description=(
                    f"Do podziału: {rozliczenie['podzial']:,} srebra"
                    if rozliczenie else f"Do wyceny • {len(content.get('uczestnicy', []))} uczestników"
                ).replace(",", " ")[:100],
                value=content_id,
            )
            for content_id, content in oczekujace
            for rozliczenie in [content.get("rozliczenie")]
        ]
        super().__init__(placeholder="Wybierz rozliczenie do sprawdzenia", min_values=1, max_values=1, options=opcje)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_content_id = self.values[0]
        content = albion_contenty.get(self.values[0], {})
        rozliczenie = content.get("rozliczenie") or {}
        lista_uczestnikow = rozliczenie.get("uczestnicy", content.get("uczestnicy", []))
        podzial_graczy = [
            f"• <@{user_id}> — {rozliczenie.get('wyplaty', {}).get(str(user_id), 0):,} srebra".replace(",", " ")
            for user_id in lista_uczestnikow[:10]
        ] if rozliczenie else []
        if len(lista_uczestnikow) > 10:
            podzial_graczy.append(f"…i {len(lista_uczestnikow) - 10} kolejnych graczy")
        if rozliczenie:
            opis = (
                f"Loot {rozliczenie.get('loot', 0):,}, koszty {rozliczenie.get('koszty', 0):,}, "
                f"do podziału {rozliczenie.get('podzial', 0):,} srebra.\n"
                f"Zgłosił: <@{rozliczenie.get('autor_id')}>. Podział:\n"
                f"{chr(10).join(podzial_graczy) or 'Brak uczestników.'}\n"
                f"Uwagi: {rozliczenie.get('uwagi') or 'brak'}"
            )
        else:
            opis = (
                f"Content po zakończeniu i usunięciu karty. Uczestnicy: {len(lista_uczestnikow)}. "
                "Ustaw kwotę do podziału, aby przygotować rozliczenie."
            )
        await interaction.response.edit_message(
            content=f"**{content.get('nazwa', 'Content')}**\n{opis}".replace(",", " "),
            view=self.parent_view,
        )


class PendingSettlementView(discord.ui.View):
    def __init__(self, guild_id, tryb_gildia=False):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.tryb_gildia = tryb_gildia
        self.selected_content_id = None
        self.add_item(PendingSettlementSelect(self))

    async def interaction_check(self, interaction):
        if interaction.guild is None or interaction.guild.id != self.guild_id or not moze_wyplacac(interaction.user):
            await interaction.response.send_message("Rozliczenia może zatwierdzać administrator lub wskazana rola.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Zatwierdź i zaksięguj", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def approve(self, interaction, button):
        content = albion_contenty.get(self.selected_content_id)
        rozliczenie = (content or {}).get("rozliczenie") or {}
        if not content or rozliczenie.get("status") != "oczekuje":
            await interaction.response.send_message("Wybierz oczekujące rozliczenie. Mogło już zostać obsłużone.", ephemeral=True)
            return

        konta_przed = {}
        for user_id, kwota in rozliczenie.get("wyplaty", {}).items():
            klucz = klucz_konta(self.guild_id, int(user_id))
            if klucz not in konta_przed:
                konta_przed[klucz] = copy.deepcopy(albion_konta.get(klucz))
            konto = albion_konta.setdefault(klucz, {"zarobione": 0, "wyplacone": 0, "transakcje": []})
            transakcje = konto.setdefault("transakcje", [])
            if not any(t.get("typ") == "content" and t.get("content_id") == content["id"] for t in transakcje):
                konto["zarobione"] = int(konto.get("zarobione", 0)) + int(kwota)
                transakcje.append({
                    "typ": "content", "content_id": content["id"], "kwota": int(kwota),
                    "czas": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
        rozliczenie["status"] = "zaakceptowane"
        rozliczenie["zaksięgowane"] = True
        rozliczenie["zatwierdzil_id"] = interaction.user.id
        rozliczenie["zatwierdzono_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            zapisz_konta()
            zapisz_contenty()
        except OSError as error:
            for klucz, konto in konta_przed.items():
                if konto is None:
                    albion_konta.pop(klucz, None)
                else:
                    albion_konta[klucz] = konto
            rozliczenie["status"] = "oczekuje"
            rozliczenie["zaksięgowane"] = False
            rozliczenie.pop("zatwierdzil_id", None)
            rozliczenie.pop("zatwierdzono_at", None)
            try:
                zapisz_konta()
            except OSError as rollback_error:
                print(f"Nie udało się utrwalić cofnięcia sald po błędzie akceptacji: {rollback_error}")
            try:
                zapisz_contenty()
            except OSError as rollback_error:
                print(f"Nie udało się utrwalić cofnięcia statusu rozliczenia: {rollback_error}")
            print(f"Nie udało się zatwierdzić rozliczenia {self.selected_content_id}: {error}")
            await interaction.response.send_message("Nie udało się zapisać akceptacji. Rozliczenie nadal oczekuje.", ephemeral=True)
            return

        try:
            kanal = bot.get_channel(int(content["channel_id"])) or await bot.fetch_channel(int(content["channel_id"]))
            karta = await kanal.fetch_message(int(content["message_id"]))
            await karta.edit(embed=zbuduj_embed_contentu(content), view=ContentView(content["id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, KeyError, TypeError, ValueError) as error:
            print(f"Zatwierdzono rozliczenie, ale nie odświeżono karty {self.selected_content_id}: {error}")
        await interaction.response.edit_message(content="Rozliczenie zaakceptowane i zaksięgowane na saldach graczy.", view=None)

    @discord.ui.button(label="Odrzuć rozliczenie", style=discord.ButtonStyle.danger, emoji="✖️", row=1)
    async def reject(self, interaction, button):
        content = albion_contenty.get(self.selected_content_id)
        rozliczenie = (content or {}).get("rozliczenie") or {}
        if not content or rozliczenie.get("status") != "oczekuje":
            await interaction.response.send_message("Wybierz oczekujące rozliczenie.", ephemeral=True)
            return
        rozliczenie["status"] = "odrzucone"
        rozliczenie["odrzucil_id"] = interaction.user.id
        rozliczenie["odrzucono_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            zapisz_contenty()
        except OSError as error:
            rozliczenie["status"] = "oczekuje"
            rozliczenie.pop("odrzucil_id", None)
            rozliczenie.pop("odrzucono_at", None)
            print(f"Nie udało się odrzucić rozliczenia {self.selected_content_id}: {error}")
            await interaction.response.send_message("Nie udało się zapisać odrzucenia.", ephemeral=True)
            return
        try:
            kanal = bot.get_channel(int(content["channel_id"])) or await bot.fetch_channel(int(content["channel_id"]))
            karta = await kanal.fetch_message(int(content["message_id"]))
            await karta.edit(embed=zbuduj_embed_contentu(content), view=ContentView(content["id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, KeyError, TypeError, ValueError) as error:
            print(f"Odrzucono rozliczenie, ale nie odświeżono karty {self.selected_content_id}: {error}")
        await interaction.response.edit_message(content="Rozliczenie odrzucone. Nie zmieniono sald graczy.", view=None)

    @discord.ui.button(label="Zmień kwotę silvera", style=discord.ButtonStyle.secondary, emoji="✏️", row=1)
    async def adjust_amount(self, interaction, button):
        content = albion_contenty.get(self.selected_content_id)
        if not content:
            await interaction.response.send_message("Najpierw wybierz content.", ephemeral=True)
            return
        rozliczenie = content.get("rozliczenie") or {}
        if self.tryb_gildia:
            if not content.get("karta_usunieta") or not content.get("rozliczenie_gildii"):
                await interaction.response.send_message("Ta pozycja nie jest archiwalnym rozliczeniem gildii.", ephemeral=True)
                return
            if rozliczenie.get("zaksięgowane"):
                await interaction.response.send_message("To rozliczenie jest już zaksięgowane i nie można zmienić kwoty.", ephemeral=True)
                return
        elif rozliczenie.get("status") != "oczekuje":
            await interaction.response.send_message("Możesz zmienić kwotę tylko w oczekującym rozliczeniu.", ephemeral=True)
            return
        await interaction.response.send_modal(KorektaSilverModal(self.selected_content_id, self.tryb_gildia))


class KorektaSilverModal(discord.ui.Modal, title="Ustaw kwotę do podziału"):
    kwota = discord.ui.TextInput(label="Łączna kwota silvera", max_length=30)

    def __init__(self, content_id, tryb_gildia=False):
        super().__init__(custom_id=f"albion:adjust-settlement:{content_id}")
        self.content_id = content_id
        self.tryb_gildia = tryb_gildia
        content = albion_contenty.get(content_id, {})
        rozliczenie = content.get("rozliczenie") or {}
        if rozliczenie.get("podzial") is not None:
            self.kwota.default = str(rozliczenie["podzial"])

    async def on_submit(self, interaction: discord.Interaction):
        content = albion_contenty.get(self.content_id)
        if not content or interaction.guild is None or interaction.guild.id != int(content.get("guild_id", 0)) or not moze_wyplacac(interaction.user):
            await interaction.response.send_message("Kwoty może zmieniać administrator lub osoba z rolą rozliczającą.", ephemeral=True)
            return
        if self.tryb_gildia and not (
            content.get("rozliczenie_gildii") and content.get("karta_usunieta") and content.get("zakonczony_at")
        ):
            await interaction.response.send_message("Ten content nie znajduje się w archiwum rozliczeń gildii.", ephemeral=True)
            return
        poprzednie = content.get("rozliczenie")
        if poprzednie and poprzednie.get("zaksięgowane"):
            await interaction.response.send_message("To rozliczenie jest już zaksięgowane.", ephemeral=True)
            return
        if not self.tryb_gildia and (poprzednie or {}).get("status") != "oczekuje":
            await interaction.response.send_message("Możesz zmienić kwotę tylko w oczekującym rozliczeniu.", ephemeral=True)
            return
        uczestnicy = content.get("uczestnicy", [])
        if not uczestnicy:
            await interaction.response.send_message("Nie ma uczestników do rozliczenia.", ephemeral=True)
            return
        try:
            kwota = int(str(self.kwota.value).replace(" ", ""))
            if kwota < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Kwota musi być nieujemną liczbą całkowitą.", ephemeral=True)
            return

        stare_rozliczenie = copy.deepcopy(poprzednie)
        teraz = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rozliczenie = poprzednie or {
            "loot": kwota,
            "koszty": 0,
            "podzial": kwota,
            "uczestnicy": list(uczestnicy),
            "uwagi": "Rozliczenie przygotowane przez osobę rozliczającą.",
            "zrodlo": "gildia",
        }
        poprzednia_kwota = rozliczenie.get("podzial")
        korekty = list(rozliczenie.get("korekty_silvera", []))
        if poprzednia_kwota is not None and int(poprzednia_kwota) != kwota:
            korekty.append({"z": int(poprzednia_kwota), "na": kwota, "autor_id": interaction.user.id, "czas": teraz})
        rozliczenie["uczestnicy"] = list(uczestnicy)
        rozliczenie["podzial"] = kwota
        rozliczenie["na_osobe"] = kwota // len(set(uczestnicy))
        rozliczenie["wyplaty"] = podziel_silver_miedzy_uczestnikow(uczestnicy, kwota)
        rozliczenie["status"] = "oczekuje"
        rozliczenie["zaksięgowane"] = False
        rozliczenie["korekty_silvera"] = korekty
        rozliczenie["zmienil_kwote_id"] = interaction.user.id
        rozliczenie["zmieniono_kwote_at"] = teraz
        if self.tryb_gildia:
            rozliczenie["loot"] = kwota
            rozliczenie["koszty"] = 0
            rozliczenie["zrodlo"] = "gildia"
            rozliczenie.setdefault("autor_id", interaction.user.id)
        content["rozliczenie"] = rozliczenie
        try:
            zapisz_contenty()
        except OSError as error:
            content["rozliczenie"] = stare_rozliczenie
            print(f"Nie udało się zmienić kwoty rozliczenia {self.content_id}: {error}")
            await interaction.response.send_message("Nie udało się zapisać zmiany kwoty.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Ustawiono **{kwota:,} srebra** łącznie ({len(set(uczestnicy))} graczy). Rozliczenie czeka na zatwierdzenie; ponownie wybierz content, aby odświeżyć podgląd.".replace(",", " "),
            ephemeral=True,
        )


class FinanceLauncherView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        akcje = {
            "Otwórz panel rozliczeń": "open",
            "Resetuj wszystkie salda": "reset",
            "Resetuj historię zarobków": "reset-history",
            "Oczekujące akceptacje": "pending",
            "Rozliczenia gildii": "guild-settlements",
        }
        for child in self.children:
            child.custom_id = f"albion:finance:{guild_id}:{akcje[child.label]}"

    @discord.ui.button(label="Otwórz panel rozliczeń", style=discord.ButtonStyle.primary, emoji="💰")
    async def open_panel(self, interaction, button):
        if interaction.guild is None or interaction.guild.id != self.guild_id or not moze_wyplacac(interaction.user):
            await interaction.response.send_message("Dostęp ma administrator lub rola 1428090511528300554.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=zbuduj_embed_sald(self.guild_id), view=PayoutPanelView(self.guild_id), ephemeral=True
        )

    @discord.ui.button(label="Oczekujące akceptacje", style=discord.ButtonStyle.success, emoji="🧾")
    async def pending_settlements(self, interaction, button):
        if interaction.guild is None or interaction.guild.id != self.guild_id or not moze_wyplacac(interaction.user):
            await interaction.response.send_message("Dostęp ma administrator lub wskazana rola rozliczająca.", ephemeral=True)
            return
        oczekujace = oczekujace_rozliczenia(self.guild_id)
        if not oczekujace:
            await interaction.response.send_message("Nie ma rozliczeń oczekujących na akceptację.", ephemeral=True)
            return
        view = PendingSettlementView(self.guild_id)
        await interaction.response.send_message(
            f"Oczekujące rozliczenia: **{len(oczekujace)}**. Wybierz jedno, sprawdź szczegóły i zaakceptuj albo odrzuć.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Rozliczenia gildii", style=discord.ButtonStyle.primary, emoji="🏦")
    async def guild_settlements(self, interaction, button):
        if interaction.guild is None or interaction.guild.id != self.guild_id or not moze_wyplacac(interaction.user):
            await interaction.response.send_message("Dostęp ma administrator lub rola 1428090511528300554.", ephemeral=True)
            return
        archiwum = archiwalne_rozliczenia_gildii(self.guild_id)
        if not archiwum:
            await interaction.response.send_message("Nie ma zakończonych contentów oczekujących w archiwum rozliczeń gildii.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Archiwum rozliczeń gildii: **{len(archiwum)}** zakończonych contentów. Wybierz content, ustaw lub popraw kwotę silvera, a następnie zatwierdź rozliczenie.",
            view=PendingSettlementView(self.guild_id, tryb_gildia=True),
            ephemeral=True,
        )

    @discord.ui.button(label="Resetuj wszystkie salda", style=discord.ButtonStyle.danger, emoji="♻️")
    async def reset_balances(self, interaction, button):
        if interaction.guild is None or interaction.guild.id != self.guild_id or not (czy_wlasciciel(interaction.user) or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Reset sald jest dostępny wyłącznie dla administratora serwera.", ephemeral=True)
            return
        await interaction.response.send_message(
            "To wyzeruje dostępne salda wszystkich graczy na tym serwerze. Historia transakcji zostanie zachowana. Potwierdzasz?",
            view=ConfirmResetView(self.guild_id, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(label="Resetuj historię zarobków", style=discord.ButtonStyle.secondary, emoji="🧹")
    async def reset_history(self, interaction, button):
        if interaction.guild is None or interaction.guild.id != self.guild_id or not (czy_wlasciciel(interaction.user) or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Reset historii jest dostępny wyłącznie dla administratora serwera.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Zarobione silver z contentów i jego historia zostaną wyzerowane. Aktualne salda oraz historia wypłat pozostaną bez zmian. Potwierdzasz?",
            view=ConfirmHistoryResetView(self.guild_id, interaction.user.id),
            ephemeral=True,
        )


class ConfirmResetView(discord.ui.View):
    def __init__(self, guild_id, admin_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.admin_id = admin_id

    async def interaction_check(self, interaction):
        if (
            interaction.guild is None
            or interaction.guild.id != self.guild_id
            or interaction.user.id != self.admin_id
            or not (czy_wlasciciel(interaction.user) or interaction.user.guild_permissions.administrator)
        ):
            await interaction.response.send_message("Potwierdzić reset może tylko administrator, który go rozpoczął.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Potwierdź reset", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm(self, interaction, button):
        liczba = 0
        prefix = f"{self.guild_id}:"
        teraz = datetime.now(timezone.utc).isoformat(timespec="seconds")
        kopie = {}
        for klucz, konto in albion_konta.items():
            if not klucz.startswith(prefix):
                continue
            korekta = int(konto.get("korekty_reset", 0))
            saldo = saldo_konta(self.guild_id, int(klucz.split(":", 1)[1]))
            if saldo == 0:
                continue
            kopie[klucz] = korekta
            konto["korekty_reset"] = korekta + saldo
            konto.setdefault("transakcje", []).append({
                "typ": "reset_salda", "kwota": saldo, "admin_id": interaction.user.id, "czas": teraz,
            })
            liczba += 1
        try:
            zapisz_konta()
        except OSError as error:
            for klucz, korekta in kopie.items():
                albion_konta[klucz]["korekty_reset"] = korekta
                albion_konta[klucz]["transakcje"].pop()
            print(f"Nie udało się zapisać resetu sald: {error}")
            await interaction.response.edit_message(content="Nie udało się zapisać resetu. Salda nie zostały zmienione.", view=None)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Zresetowano dostępne salda {liczba} graczy. Historia transakcji została zachowana.",
            view=self,
        )

    @discord.ui.button(label="Anuluj", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Reset anulowany. Salda bez zmian.", view=None)


class ConfirmHistoryResetView(discord.ui.View):
    def __init__(self, guild_id, admin_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.admin_id = admin_id

    async def interaction_check(self, interaction):
        if (
            interaction.guild is None
            or interaction.guild.id != self.guild_id
            or interaction.user.id != self.admin_id
            or not (czy_wlasciciel(interaction.user) or interaction.user.guild_permissions.administrator)
        ):
            await interaction.response.send_message("Potwierdzić reset historii może tylko administrator, który go rozpoczął.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Usuń historię zarobków", style=discord.ButtonStyle.danger, emoji="🧹")
    async def confirm(self, interaction, button):
        prefix = f"{self.guild_id}:"
        oryginalne = {}
        usuniete = 0
        for klucz, konto in albion_konta.items():
            if not klucz.startswith(prefix):
                continue
            transakcje = konto.setdefault("transakcje", [])
            zachowane = [
                wpis for wpis in transakcje
                if wpis.get("typ") not in {"content", "reset_salda"}
            ]
            roznica = len(transakcje) - len(zachowane)
            zarobione = int(konto.get("zarobione", 0))
            if not roznica and zarobione == 0:
                continue
            oryginalne[klucz] = {
                "zarobione": zarobione,
                "korekty_reset": int(konto.get("korekty_reset", 0)),
                "transakcje": transakcje,
            }
            saldo = saldo_konta(self.guild_id, int(klucz.split(":", 1)[1]))
            konto["zarobione"] = 0
            konto["korekty_reset"] = -int(konto.get("wyplacone", 0)) - saldo
            konto["transakcje"] = zachowane
            usuniete += roznica
        try:
            zapisz_konta()
        except OSError as error:
            for klucz, stan in oryginalne.items():
                albion_konta[klucz]["zarobione"] = stan["zarobione"]
                albion_konta[klucz]["korekty_reset"] = stan["korekty_reset"]
                albion_konta[klucz]["transakcje"] = stan["transakcje"]
            print(f"Nie udało się usunąć historii zarobków: {error}")
            await interaction.response.edit_message(content="Nie udało się zapisać zmian. Historia pozostała bez zmian.", view=None)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Wyzerowano historię zarobionego silvera i usunięto {usuniete} wpisów contentów. Aktualne salda i historia wypłat nie zostały zmienione.",
            view=self,
        )


async def zapewnij_panel_finansowy(guild, channel):
    klucz = f"{guild.id}:{channel.id}"
    panel = albion_panele.get(klucz)
    legacy_key = str(guild.id)
    if panel is None:
        legacy_panel = albion_panele.get(legacy_key)
        if legacy_panel and legacy_panel.get("channel_id") == channel.id:
            panel = legacy_panel
    if panel:
        try:
            kanal_panelu = bot.get_channel(panel["channel_id"]) or await bot.fetch_channel(panel["channel_id"])
            wiadomosc = await kanal_panelu.fetch_message(panel["message_id"])
            await wiadomosc.edit(
                embed=discord.Embed(
                    title="⚙️ Panel administratora — Albion",
                    description="Otwórz rozliczenia, akceptuj wypłaty i obsługuj archiwum contentów rozliczanych przez gildię. Reset sald i historii jest dostępny dla administratorów.",
                    color=discord.Color.gold(),
                ),
                view=FinanceLauncherView(guild.id),
            )
            albion_panele[klucz] = {
                "guild_id": guild.id,
                "channel_id": kanal_panelu.id,
                "message_id": wiadomosc.id,
            }
            if legacy_key != klucz:
                albion_panele.pop(legacy_key, None)
            zapisz_konta()
            return wiadomosc
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, KeyError):
            pass
    wiadomosc = await channel.send(
        embed=discord.Embed(
            title="⚙️ Panel administratora — Albion",
            description="Otwórz rozliczenia, akceptuj wypłaty i obsługuj archiwum contentów rozliczanych przez gildię. Reset sald i historii jest dostępny dla administratorów.",
            color=discord.Color.gold(),
        ),
        view=FinanceLauncherView(guild.id),
    )
    albion_panele[klucz] = {
        "guild_id": guild.id,
        "channel_id": channel.id,
        "message_id": wiadomosc.id,
    }
    zapisz_konta()
    return wiadomosc


async def opublikuj_nowy_panel_finansowy(guild, channel):
    """Publikuje świeży panel po contencie i usuwa poprzednie panele serwera."""
    wiadomosc = await channel.send(
        embed=discord.Embed(
            title="⚙️ Panel administratora — Albion",
            description="Otwórz prywatny panel wypłat, zresetuj salda lub wyczyść historię zarobków. Operacje resetu wymagają potwierdzenia administratora.",
            color=discord.Color.gold(),
        ),
        view=FinanceLauncherView(guild.id),
    )

    stare_panele = []
    for klucz, panel in list(albion_panele.items()):
        try:
            panel_guild_id = int(panel.get("guild_id") or str(klucz).split(":", 1)[0])
        except (TypeError, ValueError):
            continue
        if panel_guild_id == guild.id:
            stare_panele.append((klucz, panel))

    for klucz, panel in stare_panele:
        try:
            kanal = bot.get_channel(int(panel["channel_id"])) or await bot.fetch_channel(int(panel["channel_id"]))
            stara_wiadomosc = await kanal.fetch_message(int(panel["message_id"]))
            await stara_wiadomosc.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException, KeyError, TypeError, ValueError) as error:
            print(f"Nie udało się usunąć starego panelu rozliczeń {klucz}: {error}")
        albion_panele.pop(klucz, None)

    klucz = f"{guild.id}:{channel.id}"
    albion_panele[klucz] = {
        "guild_id": guild.id,
        "channel_id": channel.id,
        "message_id": wiadomosc.id,
    }
    zapisz_konta()
    return wiadomosc


def parsuj_podzial_rol(tekst):
    role = {}
    if not tekst.strip():
        return role
    for element in tekst.split(","):
        if ":" not in element:
            raise ValueError("Podaj role w formacie Tank:1, Healer:1, DPS:5.")
        nazwa, liczba = element.rsplit(":", 1)
        nazwa = nazwa.strip()
        if not nazwa or len(nazwa) > 30:
            raise ValueError("Nazwa roli musi mieć od 1 do 30 znaków.")
        try:
            miejsca = int(liczba.strip())
        except ValueError as error:
            raise ValueError(f"Nieprawidłowa liczba miejsc dla roli {nazwa}.") from error
        if miejsca < 1 or miejsca > 100:
            raise ValueError("Każda rola musi mieć od 1 do 100 miejsc.")
        if nazwa.casefold() in {istniejaca.casefold() for istniejaca in role}:
            raise ValueError(f"Rola {nazwa} została podana więcej niż raz.")
        role[nazwa] = miejsca
        if len(role) > 25:
            raise ValueError("Można dodać maksymalnie 25 ról.")
    return role


class ZapiszSzablonRolModal(discord.ui.Modal, title="Zapisz układ ról"):
    nazwa_szablonu = discord.ui.TextInput(label="Nazwa szablonu", max_length=40)

    def __init__(self, content_id):
        super().__init__(custom_id=f"albion:save-role-template:{content_id}")
        self.content_id = content_id

    async def on_submit(self, interaction: discord.Interaction):
        content = albion_contenty.get(self.content_id)
        if not content or interaction.guild is None:
            await interaction.response.send_message("Nie znaleziono contentu.", ephemeral=True)
            return
        if not wlasciciel_lub_organizator(interaction.user, content):
            await interaction.response.send_message("Szablon może zapisać organizator lub moderator.", ephemeral=True)
            return
        nazwa = str(self.nazwa_szablonu.value).strip()
        if not nazwa:
            await interaction.response.send_message("Podaj nazwę szablonu.", ephemeral=True)
            return
        serwerowe = albion_szablony_rol.setdefault(str(content["guild_id"]), {})
        serwerowe[nazwa] = content.get("role_slots", {"Bez roli": 100}).copy()
        zapisz_konta()
        await interaction.response.send_message(
            f"Zapisano szablon **{nazwa}**. Użyj go przy kolejnym `/content-dodaj` w polu szablonu ról.",
            ephemeral=True,
        )


class RoleSignupSelect(discord.ui.Select):
    def __init__(self, content_id, content):
        self.content_id = content_id
        role_slots = content.get("role_slots") or {"Bez roli": content.get("limit") or 100}
        role_signups = content.get("zapisy_rol", {})
        options = []
        for nazwa, limit in list(role_slots.items())[:25]:
            zapisani = role_signups.get(nazwa, [])
            pozostalo = max(0, int(limit) - len(zapisani))
            options.append(discord.SelectOption(
                label=nazwa[:100],
                description=f"{len(zapisani)}/{limit} miejsc • wolnych {pozostalo}"[:100],
                value=nazwa,
            ))
        super().__init__(
            placeholder="Wybierz rolę, aby zapisać się na content",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="Bez roli", value="Bez roli")],
            custom_id=f"albion:content:{content_id}:role-signup",
            row=1,
        )
        self.disabled = bool(content.get("zakonczony_at"))

    async def callback(self, interaction: discord.Interaction):
        content = albion_contenty.get(self.content_id)
        if not content:
            await interaction.response.send_message("Ten content już nie istnieje.", ephemeral=True)
            return
        if content.get("zakonczony_at"):
            await interaction.response.send_message("Zapisy na ten content są zamknięte.", ephemeral=True)
            return
        rola = self.values[0]
        role_slots = content.get("role_slots") or {"Bez roli": content.get("limit") or 100}
        role_signups = content.setdefault("zapisy_rol", {})
        if not role_signups and content.get("uczestnicy"):
            role_signups["Bez roli"] = content["uczestnicy"].copy()
        zapisani_w_rolach = role_signups.setdefault(rola, [])
        jest_zapisany = any(interaction.user.id in lista for lista in role_signups.values())
        if interaction.user.id not in zapisani_w_rolach and len(zapisani_w_rolach) >= int(role_slots.get(rola, 0)):
            await interaction.response.send_message(f"Rola **{rola}** jest już pełna.", ephemeral=True)
            return
        if jest_zapisany:
            for lista in role_signups.values():
                if interaction.user.id in lista:
                    lista.remove(interaction.user.id)
        zapisani_w_rolach = role_signups.setdefault(rola, [])
        if interaction.user.id not in zapisani_w_rolach:
            zapisani_w_rolach.append(interaction.user.id)
        content["uczestnicy"] = list(dict.fromkeys(uid for lista in role_signups.values() for uid in lista))
        zapisz_contenty()
        await interaction.message.edit(embed=zbuduj_embed_contentu(content), view=ContentView(self.content_id))
        await interaction.response.send_message(f"Zapisano Cię do roli **{rola}**.", ephemeral=True)


def zbuduj_embed_contentu(content):
    role_slots = content.setdefault("role_slots", {"Bez roli": content.get("limit") or 100})
    role_signups = content.setdefault("zapisy_rol", {})
    if not role_signups and content.get("uczestnicy"):
        role_signups["Bez roli"] = content["uczestnicy"].copy()
    content["uczestnicy"] = list(dict.fromkeys(uid for lista in role_signups.values() for uid in lista))
    liczba_miejsc = sum(int(limit) for limit in role_slots.values())
    liczba_zapisanych = len(content["uczestnicy"])
    zapelnienie = min(10, round(10 * liczba_zapisanych / liczba_miejsc)) if liczba_miejsc else 0
    pasek = "🟩" * zapelnienie + "⬛" * (10 - zapelnienie)
    kolor = discord.Color.red() if content.get("odwolany") else (
        discord.Color.dark_grey() if content.get("zakonczony_at") else discord.Color.gold()
    )
    embed = discord.Embed(
        title=content["nazwa"],
        description=f"{content.get('opis') or 'Brak dodatkowego opisu.'}\n\n**Wybierz rolę z menu poniżej, aby dołączyć.**",
        color=kolor,
    )
    embed.set_author(name="ALBION ONLINE  •  PLAN CONTENTU")
    embed.add_field(name="📅 Termin", value=content["termin"], inline=True)
    embed.add_field(name="⚔️ Rodzaj", value=content["typ"], inline=True)
    embed.add_field(name="📊 Min. IP", value=content.get("ip") or "Bez wymagań", inline=True)
    embed.add_field(
        name=f"👥 Zapisy  ·  {liczba_zapisanych}/{liczba_miejsc}",
        value=f"{pasek}\n**{liczba_zapisanych}** zapisanych  •  **{max(0, liczba_miejsc - liczba_zapisanych)}** wolnych miejsc",
        inline=False,
    )
    if content.get("zakonczony_at"):
        embed.add_field(name="🏁 Status", value="Zakończony · karta zostanie usunięta za 2 godziny", inline=False)
    elif content.get("odwolany"):
        embed.add_field(name="❌ Status", value="Content odwołany", inline=False)
    if content.get("thread_id"):
        embed.add_field(name="🧵 Dyskusja", value=f"Przejdź do <#{content['thread_id']}>", inline=True)
    embed.add_field(name="🧭 Organizator", value=f"<@{content['organizator_id']}>", inline=True)
    if content.get("rozliczenie_gildii"):
        embed.add_field(name="🏦 Rozliczenie", value="Przez gildię · po zakończeniu trafi do panelu rozliczających", inline=False)
    if content.get("role_ping_sent_at"):
        embed.add_field(name="🔔 Powiadomienia", value="Role zostały powiadomione.", inline=True)
    role_lines = []
    for rola, limit in list(role_slots.items())[:5]:
        osoby = role_signups.get(rola, [])
        stan = "✅" if len(osoby) >= int(limit) else "▫️"
        wzmianki = " · ".join(f"<@{uid}>" for uid in osoby[:3])
        if len(osoby) > 3:
            wzmianki += f" · +{len(osoby) - 3}"
        role_lines.append(f"{stan} **{rola}**  `{len(osoby)}/{limit}`" + (f"\n　{wzmianki}" if wzmianki else ""))
    if len(role_slots) > 5:
        role_lines.append(f"…i {len(role_slots) - 5} kolejnych ról")
    embed.add_field(name="🛡️ Miejsca według roli", value="\n".join(role_lines) or "Brak ról", inline=False)
    rozliczenie = content.get("rozliczenie")
    if rozliczenie:
        wyplaty = rozliczenie.get("wyplaty", {})
        odbiorcy = rozliczenie.get("uczestnicy", [])
        odbiorcy_tekst = "\n".join(
            f"<@{uid}> — {wyplaty.get(str(uid), 0):,} srebra".replace(",", " ")
            for uid in odbiorcy[:15]
        ) or "Brak zapisanych uczestników"
        if len(odbiorcy) > 15:
            odbiorcy_tekst += f"\n…i {len(odbiorcy) - 15} kolejnych"
        status_rozliczenia = {
            "oczekuje": "oczekuje na akceptację",
            "zaakceptowane": "zaakceptowane i zaksięgowane",
            "odrzucone": "odrzucone",
        }.get(rozliczenie.get("status"), "zaksięgowane" if rozliczenie.get("zaksięgowane") else "niezaksięgowane")
        embed.add_field(
            name="💰 Rozliczenie",
            value=(f"Loot: **{rozliczenie['loot']}** | Koszty: **{rozliczenie['koszty']}**\n"
                   f"Do podziału: **{rozliczenie['podzial']}**\n"
                   f"Przypisane graczom:\n{odbiorcy_tekst}\n"
                   f"Status: **{status_rozliczenia}**\n"
                   f"{rozliczenie.get('uwagi') or ''}"),
            inline=False,
        )
    embed.set_footer(text=f"Albion • Content #{content['id']}" + (f" • {rozliczenie.get('status', 'rozliczenie')}" if rozliczenie else ""))
    return embed


async def odswiez_karte_contentu(interaction, content):
    content["uczestnicy"] = content.get("uczestnicy", [])
    zapisz_contenty()
    await interaction.message.edit(embed=zbuduj_embed_contentu(content), view=ContentView(content["id"]))


class RozliczenieModal(discord.ui.Modal, title="Rozliczenie contentu"):
    loot = discord.ui.TextInput(label="Wartość lootu (srebro)", max_length=30)
    koszty = discord.ui.TextInput(label="Koszty (srebro)", default="0", max_length=30)
    uwagi = discord.ui.TextInput(label="Uwagi", required=False, style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, content_id):
        super().__init__(custom_id=f"albion:settle-modal:{content_id}")
        self.content_id = content_id

    async def on_submit(self, interaction: discord.Interaction):
        content = albion_contenty.get(self.content_id)
        if not content:
            await interaction.response.send_message("Nie znaleziono tego contentu.", ephemeral=True)
            return
        if not wlasciciel_lub_organizator(interaction.user, content):
            await interaction.response.send_message("Rozliczyć może organizator lub moderator.", ephemeral=True)
            return
        poprzednie_rozliczenie = content.get("rozliczenie") or {}
        if poprzednie_rozliczenie.get("zaksięgowane"):
            await interaction.response.send_message("Ten content został już rozliczony i zaksięgowany.", ephemeral=True)
            return
        if poprzednie_rozliczenie.get("status") == "oczekuje":
            await interaction.response.send_message("To rozliczenie już czeka na akceptację w panelu rozliczeń.", ephemeral=True)
            return
        try:
            loot = int(str(self.loot.value).replace(" ", ""))
            koszty = int(str(self.koszty.value).replace(" ", ""))
            if loot < 0 or koszty < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Loot i koszty muszą być nieujemnymi liczbami całkowitymi.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        zapisani = content.get("uczestnicy", [])
        podzial = max(0, loot - koszty)
        odbiorcy = sorted(set(zapisani))
        kwota_na_osobe, reszta = divmod(podzial, len(odbiorcy)) if odbiorcy else (0, 0)
        wyplaty = {
            str(user_id): kwota_na_osobe + (1 if index < reszta else 0)
            for index, user_id in enumerate(odbiorcy)
        }

        content["rozliczenie"] = {
            "loot": loot, "koszty": koszty, "podzial": podzial,
            "na_osobe": kwota_na_osobe,
            "uczestnicy": odbiorcy, "wyplaty": wyplaty,
            "uwagi": str(self.uwagi.value), "zaksięgowane": False,
            "status": "oczekuje",
            "autor_id": interaction.user.id,
            "zgloszono_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            zapisz_contenty()
        except OSError as error:
            print(f"Nie udało się zapisać rozliczenia contentu {self.content_id}: {error}")
            content["rozliczenie"] = poprzednie_rozliczenie or None
            await interaction.followup.send("Nie udało się zapisać rozliczenia. Salda nie zostały zmienione.", ephemeral=True)
            return

        try:
            kanal = bot.get_channel(content["channel_id"])
            if kanal is None:
                kanal = await bot.fetch_channel(content["channel_id"])
            wiadomosc = await kanal.fetch_message(content["message_id"])
            await wiadomosc.edit(embed=zbuduj_embed_contentu(content), view=ContentView(self.content_id))
        except (discord.HTTPException, KeyError) as error:
            print(f"Rozliczenie {self.content_id} zapisano, ale nie udało się odświeżyć karty: {error}")
            await interaction.followup.send("Rozliczenie oczekuje na akceptację, ale nie udało się odświeżyć karty contentu. Sprawdź je w panelu rozliczeń.", ephemeral=True)
            return
        await interaction.followup.send("Rozliczenie wysłano do akceptacji. Salda graczy zmienią się dopiero po zatwierdzeniu przez osobę rozliczającą.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"Błąd formularza rozliczenia contentu {self.content_id}: {error!r}")
        wiadomosc = "Wystąpił błąd podczas rozliczania. Zajrzyj do konsoli bota po szczegóły."
        if interaction.response.is_done():
            await interaction.followup.send(wiadomosc, ephemeral=True)
        else:
            await interaction.response.send_message(wiadomosc, ephemeral=True)


class ContentView(discord.ui.View):
    def __init__(self, content_id):
        super().__init__(timeout=None)
        self.content_id = content_id
        content = albion_contenty.get(content_id, {})
        self.add_item(RoleSignupSelect(content_id, content))
        akcje = {"Wypisz się": "leave", "Panel organizatora": "organizer-panel"}
        for button in self.children:
            if isinstance(button, discord.ui.Button):
                button.custom_id = f"albion:content:{content_id}:{akcje[button.label]}"
        zakonczony = bool(content.get("zakonczony_at"))
        for button in self.children:
            if button.custom_id.endswith((":leave", ":role-signup")):
                button.disabled = zakonczony

    async def interaction_check(self, interaction):
        if self.content_id not in albion_contenty:
            await interaction.response.send_message("Ten content już nie istnieje.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Wypisz się", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def leave(self, interaction, button):
        content = albion_contenty[self.content_id]
        for lista in content.get("zapisy_rol", {}).values():
            if interaction.user.id in lista:
                lista.remove(interaction.user.id)
        content["uczestnicy"] = [uid for uid in content.get("uczestnicy", []) if uid != interaction.user.id]
        await odswiez_karte_contentu(interaction, content)
        await interaction.response.send_message("Wypisano Cię z contentu.", ephemeral=True)

    @discord.ui.button(label="Panel organizatora", style=discord.ButtonStyle.primary, emoji="⚙️", row=2)
    async def organizer_panel(self, interaction, button):
        content = albion_contenty[self.content_id]
        if not wlasciciel_lub_organizator(interaction.user, content):
            await interaction.response.send_message("Panel organizatora jest dostępny dla organizatora i moderatorów.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**Panel organizatora — {content['nazwa']}**\nWybierz gracza i rolę z list, a potem kliknij **Zapisz gracza**. Możesz też ręcznie pingnąć role, rozliczyć i zarządzać contentem.",
            view=OrganizerPanelView(self.content_id),
            ephemeral=True,
        )


class OrganizerMemberSelect(discord.ui.UserSelect):
    def __init__(self, parent_view):
        super().__init__(
            placeholder="1. Wybierz gracza do zapisania",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_user_id = self.values[0].id
        await interaction.response.defer(ephemeral=True)


class OrganizerRoleSelect(discord.ui.Select):
    def __init__(self, parent_view, content):
        self.parent_view = parent_view
        role_slots = content.get("role_slots") or {"Bez roli": content.get("limit") or 100}
        role_signups = content.get("zapisy_rol", {})
        options = [
            discord.SelectOption(
                label=rola[:100],
                description=f"{len(role_signups.get(rola, []))}/{limit} miejsc"[:100],
                value=rola,
            )
            for rola, limit in role_slots.items()
        ][:25]
        super().__init__(
            placeholder="2. Wybierz rolę",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="Bez roli", value="Bez roli")],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_role = self.values[0]
        await interaction.response.defer(ephemeral=True)


class OrganizerPanelView(discord.ui.View):
    def __init__(self, content_id):
        super().__init__(timeout=900)
        self.content_id = content_id
        self.selected_user_id = None
        self.selected_role = None
        content = albion_contenty.get(content_id, {})
        self.add_item(OrganizerMemberSelect(self))
        self.add_item(OrganizerRoleSelect(self, content))
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label.startswith("Rozliczenie gildii:"):
                item.label = f"Rozliczenie gildii: {'TAK' if content.get('rozliczenie_gildii') else 'NIE'}"
                item.disabled = bool(content.get("zakonczony_at") or content.get("odwolany"))
        if content.get("role_ping_sent_at"):
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.label == "Pingnij role":
                    item.disabled = True

    async def interaction_check(self, interaction):
        content = albion_contenty.get(self.content_id)
        if not content:
            await interaction.response.send_message("Ten content już nie istnieje.", ephemeral=True)
            return False
        if interaction.guild is None or not wlasciciel_lub_organizator(interaction.user, content):
            await interaction.response.send_message("Dostęp ma organizator lub moderator.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Rozliczenie", style=discord.ButtonStyle.primary, emoji="💰", row=2)
    async def settle(self, interaction, button):
        content = albion_contenty[self.content_id]
        if content.get("rozliczenie_gildii"):
            await interaction.response.send_message(
                "Ten content jest rozliczany przez gildię. Po zakończeniu i usunięciu karty pojawi się w panelu administratora → Rozliczenia gildii.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(RozliczenieModal(self.content_id))

    @discord.ui.button(label="Rozliczenie gildii: NIE", style=discord.ButtonStyle.secondary, emoji="🏦", row=4)
    async def guild_settlement_toggle(self, interaction, button):
        content = albion_contenty[self.content_id]
        if content.get("zakonczony_at") or content.get("odwolany"):
            await interaction.response.send_message("Tryb rozliczenia gildyjnego można ustawić przed zakończeniem contentu.", ephemeral=True)
            return
        if (content.get("rozliczenie") or {}).get("zaksięgowane"):
            await interaction.response.send_message("Content ma już zaksięgowane rozliczenie i nie można zmienić jego trybu.", ephemeral=True)
            return
        content["rozliczenie_gildii"] = not bool(content.get("rozliczenie_gildii"))
        try:
            zapisz_contenty()
        except OSError as error:
            content["rozliczenie_gildii"] = not content["rozliczenie_gildii"]
            print(f"Nie udało się zmienić trybu rozliczenia gildyjnego {self.content_id}: {error}")
            await interaction.response.send_message("Nie udało się zapisać ustawienia.", ephemeral=True)
            return
        stan = "włączone" if content["rozliczenie_gildii"] else "wyłączone"
        await interaction.response.edit_message(
            content=f"**Panel organizatora — {content['nazwa']}**\nRozliczenie przez gildię: **{stan}**.",
            view=OrganizerPanelView(self.content_id),
        )

    @discord.ui.button(label="Zapisz gracza", style=discord.ButtonStyle.success, emoji="➕", row=2)
    async def add_player(self, interaction, button):
        content = albion_contenty[self.content_id]
        if content.get("zakonczony_at"):
            await interaction.response.send_message("Zapisy na ten content są już zamknięte.", ephemeral=True)
            return
        if self.selected_user_id is None or self.selected_role is None:
            await interaction.response.send_message("Najpierw wybierz gracza i rolę z list powyżej.", ephemeral=True)
            return
        role_slots = content.get("role_slots") or {"Bez roli": content.get("limit") or 100}
        if self.selected_role not in role_slots:
            await interaction.response.send_message("Wybrana rola nie jest już dostępna.", ephemeral=True)
            return
        user_id = self.selected_user_id
        rola = self.selected_role
        zapisy_rol = content.setdefault("zapisy_rol", {})
        if not zapisy_rol and content.get("uczestnicy"):
            zapisy_rol["Bez roli"] = content["uczestnicy"].copy()
        sklad_rol = zapisy_rol.setdefault(rola, [])
        if user_id not in sklad_rol and len(sklad_rol) >= int(role_slots[rola]):
            await interaction.response.send_message(f"Rola **{rola}** jest już pełna.", ephemeral=True)
            return
        for osoby in zapisy_rol.values():
            if user_id in osoby:
                osoby.remove(user_id)
        zapisy_rol.setdefault(rola, []).append(user_id)
        content["uczestnicy"] = list(dict.fromkeys(uid for osoby in zapisy_rol.values() for uid in osoby))
        zapisz_contenty()
        self.selected_user_id = None
        self.selected_role = None
        await interaction.response.defer(ephemeral=True)
        kanal = bot.get_channel(content["channel_id"])
        if kanal is None:
            kanal = await bot.fetch_channel(content["channel_id"])
        wiadomosc = await kanal.fetch_message(content["message_id"])
        await wiadomosc.edit(embed=zbuduj_embed_contentu(content), view=ContentView(self.content_id))
        await interaction.edit_original_response(
            content="Zapisano gracza do składu. Możesz wybrać kolejną osobę.",
            view=OrganizerPanelView(self.content_id),
        )

    @discord.ui.button(label="Pingnij role", style=discord.ButtonStyle.primary, emoji="📣", row=2)
    async def ping_roles(self, interaction, button):
        content = albion_contenty[self.content_id]
        if content.get("role_ping_sent_at"):
            await interaction.response.send_message("Role zostały już powiadomione dla tego contentu.", ephemeral=True)
            return
        if content.get("zakonczony_at") or content.get("odwolany"):
            await interaction.response.send_message("Nie można powiadomić ról o zakończonym lub odwołanym contencie.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        kanal = bot.get_channel(content["channel_id"])
        if kanal is None:
            kanal = await bot.fetch_channel(content["channel_id"])
        wzmianki = " ".join(f"<@&{role_id}>" for role_id in CONTENT_PING_ROLE_IDS)
        try:
            ping_wiadomosc = await kanal.send(
                f"📣 Nowy content: **{content['nazwa']}** — {wzmianki}",
                allowed_mentions=discord.AllowedMentions(
                    roles=[discord.Object(id=role_id) for role_id in CONTENT_PING_ROLE_IDS],
                    users=False,
                    everyone=False,
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Nie udało się powiadomić ról dla contentu {self.content_id}: {error}")
            await interaction.followup.send("Nie udało się wysłać pingów. Sprawdź uprawnienia bota do wspominania ról.", ephemeral=True)
            return

        content["role_ping_sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        content["role_ping_message_id"] = ping_wiadomosc.id
        try:
            zapisz_contenty()
        except OSError as error:
            print(f"Nie udało się zapisać stanu pingowania dla contentu {self.content_id}: {error}")
            await interaction.followup.send("Ping został wysłany, ale nie udało się zapisać jego statusu. Nie klikaj ponownie.", ephemeral=True)
            return
        try:
            wiadomosc = await kanal.fetch_message(content["message_id"])
            await wiadomosc.edit(embed=zbuduj_embed_contentu(content), view=ContentView(self.content_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            print(f"Nie udało się odświeżyć karty po pingowaniu ról: {error}")
        await interaction.edit_original_response(
            content="Role zostały powiadomione. Ten przycisk jest już wyłączony.",
            view=OrganizerPanelView(self.content_id),
        )

    @discord.ui.button(label="Zapisz role jako szablon", style=discord.ButtonStyle.secondary, emoji="📋", row=3)
    async def save_template(self, interaction, button):
        await interaction.response.send_modal(ZapiszSzablonRolModal(self.content_id))

    @discord.ui.button(label="Zakończ content", style=discord.ButtonStyle.danger, emoji="🏁", row=3)
    async def end(self, interaction, button):
        content = albion_contenty[self.content_id]
        if content.get("zakonczony_at"):
            await interaction.response.send_message("Ten content jest już zakończony.", ephemeral=True)
            return
        content["zakonczony_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        zapisz_contenty()
        zmiana_contentow.set()
        await interaction.response.defer(ephemeral=True)
        kanal = bot.get_channel(content["channel_id"]) or await bot.fetch_channel(content["channel_id"])
        wiadomosc = await kanal.fetch_message(content["message_id"])
        await wiadomosc.edit(embed=zbuduj_embed_contentu(content), view=ContentView(self.content_id))
        await interaction.edit_original_response(content="Content zakończony. Karta i wątek znikną za 2 godziny.", view=None)

    @discord.ui.button(label="Odwołaj content", style=discord.ButtonStyle.danger, emoji="🗑️", row=3)
    async def cancel(self, interaction, button):
        content = albion_contenty[self.content_id]
        content["odwolany"] = True
        zapisz_contenty()
        await interaction.response.defer(ephemeral=True)
        kanal = bot.get_channel(content["channel_id"]) or await bot.fetch_channel(content["channel_id"])
        wiadomosc = await kanal.fetch_message(content["message_id"])
        await wiadomosc.edit(content="❌ Content został odwołany.", embed=zbuduj_embed_contentu(content), view=None)
        await interaction.edit_original_response(content="Content odwołany.", view=None)


async def sprzataj_zakonczone_contenty():
    await bot.wait_until_ready()
    while not bot.is_closed():
        zmiana_contentow.clear()
        teraz = datetime.now(timezone.utc)
        nastepne_usuniecie = None
        wygasle = []

        for content_id, content in list(albion_contenty.items()):
            if content.get("karta_usunieta"):
                continue
            zakonczony_at = content.get("zakonczony_at")
            if not zakonczony_at:
                continue
            try:
                koniec = datetime.fromisoformat(zakonczony_at)
                if koniec.tzinfo is None:
                    koniec = koniec.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            usun_o = koniec + timedelta(hours=2)
            if usun_o <= teraz:
                wygasle.append((content_id, content))
            elif nastepne_usuniecie is None or usun_o < nastepne_usuniecie:
                nastepne_usuniecie = usun_o

        for content_id, content in wygasle:
            try:
                thread_id = content.get("thread_id")
                if thread_id:
                    thread = bot.get_channel(thread_id)
                    if thread is None:
                        try:
                            thread = await bot.fetch_channel(thread_id)
                        except discord.NotFound:
                            thread = None
                    if isinstance(thread, discord.Thread) and not thread.archived:
                        try:
                            await thread.edit(archived=True)
                        except (discord.Forbidden, discord.HTTPException) as error:
                            print(f"Nie udało się zarchiwizować wątku contentu {content_id}: {error}")
                kanal = bot.get_channel(content.get("channel_id"))
                if kanal is None:
                    kanal = await bot.fetch_channel(content["channel_id"])
                wiadomosc = await kanal.fetch_message(content["message_id"])
                await wiadomosc.delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException, KeyError) as error:
                print(f"Nie udało się usunąć zakończonego contentu {content_id}: {error}")
                continue
            rozliczenie = content.get("rozliczenie") or {}
            if content.get("rozliczenie_gildii") or rozliczenie.get("status") == "oczekuje":
                content["karta_usunieta"] = True
                content["przeniesiono_do_rozliczen_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            else:
                albion_contenty.pop(content_id, None)
            zapisz_contenty()

        if nastepne_usuniecie is None:
            timeout = 3600
        else:
            timeout = max(0.1, (nastepne_usuniecie - datetime.now(timezone.utc)).total_seconds())
        try:
            await asyncio.wait_for(zmiana_contentow.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


def formatuj_fame(wartosc):
    while isinstance(wartosc, dict):
        if "Total" in wartosc:
            wartosc = wartosc["Total"]
        elif "All" in wartosc:
            wartosc = wartosc["All"]
        else:
            liczby = [
                element
                for element in wartosc.values()
                if isinstance(element, (int, float))
            ]
            if not liczby:
                return "Brak danych"
            wartosc = sum(liczby)

    try:
        return f"{int(wartosc):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "Brak danych"


def znajdz_pole(obiekt, *nazwy):
    if isinstance(obiekt, dict):
        for nazwa_pola in nazwy:
            wartosc = obiekt.get(nazwa_pola)
            if wartosc is not None:
                return wartosc

        for wartosc in obiekt.values():
            wynik = znajdz_pole(wartosc, *nazwy)
            if wynik is not None:
                return wynik

    elif isinstance(obiekt, list):
        for element in obiekt:
            wynik = znajdz_pole(element, *nazwy)
            if wynik is not None:
                return wynik

    return None


async def pobierz_dane_albion(session, endpoint, params=None):
    url = f"{ALBION_API}/{endpoint}"

    async with session.get(url, params=params) as response:
        if response.status != 200:
            raise RuntimeError(
                f"API Albion Online zwróciło HTTP {response.status}."
            )
        return await response.json()


def odczytaj_historie_gildii():
    try:
        with open(ALBION_HISTORY_FILE, "r", encoding="utf-8") as plik:
            return json.load(plik)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def zapisz_historie_gildii(player_id, guild_name, guild_id):
    historia = odczytaj_historie_gildii()
    wpisy = historia.setdefault(str(player_id), [])

    nazwa_gildii = guild_name or "Brak gildii"
    id_gildii = guild_id or None

    if wpisy:
        ostatni = wpisy[-1]
        if (
            ostatni.get("guild_name") == nazwa_gildii
            and ostatni.get("guild_id") == id_gildii
        ):
            return wpisy

        ostatni["do"] = datetime.now(timezone.utc).isoformat(
            timespec="minutes"
        )

    wpisy.append(
        {
            "guild_name": nazwa_gildii,
            "guild_id": id_gildii,
            "od": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "do": None,
        }
    )

    try:
        with open(ALBION_HISTORY_FILE, "w", encoding="utf-8") as plik:
            json.dump(historia, plik, ensure_ascii=False, indent=2)
    except OSError as error:
        print(f"Nie udało się zapisać historii gildii: {error}")

    return wpisy


# ---------- Grupa /albion: profile i rynek ----------

@albion.command(name="gildia", description="Podaj nazwę gildii Albion, aby zobaczyć jej statystyki.")
@app_commands.describe(nazwa="Pełna lub częściowa nazwa gildii, np. My Guild.")
async def albion_gildia(interaction: discord.Interaction, nazwa: str):
    await interaction.response.defer()
    timeout_value = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout_value) as session:
            wyniki = await pobierz_dane_albion(
                session,
                "search",
                params={"q": nazwa},
            )
            gildie = wyniki.get("guilds", [])

            if not gildie:
                await interaction.followup.send(
                    f"Nie znaleziono gildii **{nazwa}**."
                )
                return

            gildia = next(
                (
                    wynik
                    for wynik in gildie
                    if wynik.get("Name", "").casefold() == nazwa.casefold()
                ),
                gildie[0],
            )
            guild_id = gildia.get("Id")

            if not guild_id:
                await interaction.followup.send(
                    "API nie zwróciło ID gildii."
                )
                return

            dane = await pobierz_dane_albion(
                session,
                f"guilds/{guild_id}",
            )

            try:
                statystyki = await pobierz_dane_albion(
                    session,
                    f"guilds/{guild_id}/data",
                )
            except RuntimeError:
                statystyki = {}

            try:
                czlonkowie = await pobierz_dane_albion(
                    session,
                    f"guilds/{guild_id}/members",
                )
            except RuntimeError:
                czlonkowie = []

            alliance_id = dane.get("AllianceId")
            alliance_name = dane.get("AllianceName")

            if not alliance_name and alliance_id:
                try:
                    sojusz = await pobierz_dane_albion(
                        session,
                        f"alliances/{alliance_id}",
                    )
                    alliance_name = (
                        sojusz.get("Name") or sojusz.get("AllianceName")
                    )
                except RuntimeError:
                    alliance_name = None

    except (aiohttp.ClientError, TimeoutError, RuntimeError) as error:
        await interaction.followup.send(
            f"Nie udało się pobrać danych gildii: {error}"
        )
        return

    if not isinstance(czlonkowie, list):
        czlonkowie = []

    kill_fame = znajdz_pole(
        statystyki,
        "TotalKillFame",
        "KillFame",
        "TotalKillerKillFame",
    )
    death_fame = znajdz_pole(
        statystyki,
        "TotalDeathFame",
        "DeathFame",
        "TotalVictimKillFame",
    )

    if kill_fame is None:
        wartosci = [
            czlonek["KillFame"]
            for czlonek in czlonkowie
            if isinstance(czlonek, dict)
            and isinstance(czlonek.get("KillFame"), (int, float))
        ]
        if wartosci:
            kill_fame = sum(wartosci)

    if death_fame is None:
        wartosci = [
            czlonek["DeathFame"]
            for czlonek in czlonkowie
            if isinstance(czlonek, dict)
            and isinstance(czlonek.get("DeathFame"), (int, float))
        ]
        if wartosci:
            death_fame = sum(wartosci)

    try:
        if kill_fame is None or death_fame is None:
            fame_ratio = "Brak danych"
        elif float(death_fame) == 0:
            fame_ratio = "∞" if float(kill_fame) > 0 else "Brak danych"
        else:
            fame_ratio = f"{float(kill_fame) / float(death_fame):.2f}"
    except (TypeError, ValueError):
        fame_ratio = "Brak danych"

    embed = discord.Embed(
        title=dane.get("Name", gildia.get("Name", nazwa)),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Sojusz",
        value=alliance_name or "Brak danych lub gildia bez sojuszu",
        inline=True,
    )
    embed.add_field(
        name="Członkowie",
        value=str(dane.get("MemberCount", len(czlonkowie))),
        inline=True,
    )
    embed.add_field(
        name="Założona",
        value=str(dane.get("Founded") or "Brak danych"),
        inline=True,
    )
    embed.add_field(
        name="Kill fame",
        value=formatuj_fame(kill_fame),
        inline=True,
    )
    embed.add_field(
        name="Death fame",
        value=formatuj_fame(death_fame),
        inline=True,
    )
    embed.add_field(
        name="Fame ratio",
        value=fame_ratio,
        inline=True,
    )

    nazwy_czlonkow = [
        str(czlonek["Name"])
        for czlonek in czlonkowie
        if isinstance(czlonek, dict) and czlonek.get("Name")
    ]
    if nazwy_czlonkow:
        lista = "\n".join(f"• {imie}" for imie in nazwy_czlonkow[:10])
        if len(nazwy_czlonkow) > 10:
            lista += f"\n…i jeszcze {len(nazwy_czlonkow) - 10} osób"
        embed.add_field(
            name="Lista członków",
            value=lista,
            inline=False,
        )

    embed.set_footer(text=f"Guild ID: {guild_id}")
    await interaction.followup.send(embed=embed)


@albion.command(name="gracz", description="Podaj nick gracza Albion, aby zobaczyć jego fame i gildię.")
@app_commands.describe(nazwa="Nick postaci w Albion Online; najlepiej wpisz dokładną nazwę.")
async def albion_gracz(interaction: discord.Interaction, nazwa: str):
    await interaction.response.defer()
    timeout_value = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout_value) as session:
            wyniki = await pobierz_dane_albion(
                session,
                "search",
                params={"q": nazwa},
            )
            gracze = wyniki.get("players", [])

            if not gracze:
                await interaction.followup.send(
                    f"Nie znaleziono gracza **{nazwa}**."
                )
                return

            gracz = next(
                (
                    wynik
                    for wynik in gracze
                    if wynik.get("Name", "").casefold() == nazwa.casefold()
                ),
                gracze[0],
            )
            player_id = gracz.get("Id")

            if not player_id:
                await interaction.followup.send(
                    "API nie zwróciło ID gracza."
                )
                return

            dane = await pobierz_dane_albion(
                session,
                f"players/{player_id}",
            )

    except (aiohttp.ClientError, TimeoutError, RuntimeError) as error:
        await interaction.followup.send(
            f"Nie udało się pobrać danych gracza: {error}"
        )
        return

    statystyki = dane.get("LifetimeStatistics") or {}
    historia = zapisz_historie_gildii(
        player_id,
        dane.get("GuildName"),
        dane.get("GuildId"),
    )

    embed = discord.Embed(
        title=dane.get("Name", gracz.get("Name", nazwa)),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Obecna gildia",
        value=dane.get("GuildName") or "Brak",
        inline=True,
    )
    embed.add_field(
        name="Sojusz",
        value=dane.get("AllianceName") or "Brak",
        inline=True,
    )
    embed.add_field(
        name="Średnie IP",
        value=str(dane.get("AverageItemPower") or "Brak danych"),
        inline=True,
    )
    embed.add_field(
        name="Kill fame",
        value=formatuj_fame(dane.get("KillFame")),
        inline=True,
    )
    embed.add_field(
        name="Death fame",
        value=formatuj_fame(dane.get("DeathFame")),
        inline=True,
    )
    embed.add_field(
        name="PvE fame",
        value=formatuj_fame(statystyki.get("PvE")),
        inline=True,
    )
    embed.add_field(
        name="Gathering fame",
        value=formatuj_fame(statystyki.get("Gathering")),
        inline=True,
    )
    embed.add_field(
        name="Crafting fame",
        value=formatuj_fame(statystyki.get("Crafting")),
        inline=True,
    )
    embed.add_field(
        name="Fishing fame",
        value=formatuj_fame(statystyki.get("FishingFame")),
        inline=True,
    )
    embed.add_field(
        name="Farming fame",
        value=formatuj_fame(statystyki.get("FarmingFame")),
        inline=True,
    )
    embed.set_footer(
        text=f"Player ID: {player_id} • Historia zapisuje się przy użyciu /gracz"
    )

    await interaction.followup.send(embed=embed)

    linie_historii = []
    for wpis in reversed(historia):
        data_od = wpis.get("od", "")[:10]
        data_do = wpis.get("do")
        okres = (
            f"{data_od} – {data_do[:10]}"
            if data_do
            else f"{data_od} – obecnie"
        )
        linie_historii.append(
            f"• **{wpis.get('guild_name', 'Nieznana gildia')}** ({okres})"
        )

    if linie_historii and interaction.channel:
        naglowek = "**Historia gildii zapisana przez bota:**\n"
        fragment = ""

        for linia in linie_historii:
            nowy_fragment = f"{fragment}\n{linia}" if fragment else linia

            if len(nowy_fragment) > 1700 and fragment:
                await interaction.channel.send(naglowek + fragment)
                fragment = linia
            else:
                fragment = nowy_fragment

        if fragment:
            await interaction.channel.send(naglowek + fragment)


@albion.command(
    name="cena",
    description="Sprawdź aktualne ceny przedmiotu w mieście i na wybranym serwerze Albion.",
)
@app_commands.describe(
    item_id="ID przedmiotu, np. T4_BAG albo T6_MAIN_SPEAR.",
    miasto="Miasto rynku; domyślnie Caerleon.",
    serwer="Świat Albion, na którym sprawdzić rynek.",
)
@app_commands.choices(serwer=[
    app_commands.Choice(name="Europe", value="europe"),
    app_commands.Choice(name="Americas (West)", value="west"),
    app_commands.Choice(name="Asia (East)", value="east"),
])
async def albion_cena(
    interaction: discord.Interaction,
    item_id: str,
    miasto: str = "Caerleon",
    serwer: str = "europe",
):
    await interaction.response.defer()

    item_id = item_id.strip()
    miasto = miasto.strip()
    bezpieczny_id = quote(item_id, safe="_@,")
    parametry = {
        "locations": miasto,
        "qualities": "1,2,3,4,5",
    }
    timeout_value = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout_value) as session:
            async with session.get(
                f"{ALBION_MARKET_APIS[serwer]}/stats/prices/{bezpieczny_id}.json",
                params=parametry,
            ) as response:
                if response.status != 200:
                    await interaction.followup.send(
                        f"API rynku zwróciło HTTP {response.status}."
                    )
                    return
                wyniki = await response.json()

    except (aiohttp.ClientError, TimeoutError) as error:
        await interaction.followup.send(
            f"Nie udało się pobrać cen: {error}"
        )
        return

    nazwy_jakosci = {
        1: "Zwykła",
        2: "Dobra",
        3: "Wybitna",
        4: "Doskonała",
        5: "Mistrzowska",
    }

    embed = discord.Embed(
        title=f"Ceny: {item_id}",
        description=f"Miasto: **{miasto}** • Serwer: **{serwer.upper()}**",
        color=discord.Color.gold(),
    )

    aktywne_oferty = 0
    for wpis in wyniki:
        jakosc = wpis.get("quality", 1)
        kupno = int(wpis.get("buy_price_max") or 0)
        sprzedaz = int(wpis.get("sell_price_min") or 0)
        if kupno <= 0 and sprzedaz <= 0:
            continue
        aktywne_oferty += 1

        data_kupna = wpis.get("buy_price_max_date")
        data_sprzedazy = wpis.get("sell_price_min_date")
        if not data_kupna or str(data_kupna).startswith("0001-"):
            data_kupna = "brak"
        if not data_sprzedazy or str(data_sprzedazy).startswith("0001-"):
            data_sprzedazy = "brak"

        wartosc = (
            f"**Najwyższa oferta kupna:** {f'{kupno:,}'.replace(',', ' ') + ' srebra' if kupno > 0 else 'brak ofert'}\n"
            f"**Najniższa oferta sprzedaży:** {f'{sprzedaz:,}'.replace(',', ' ') + ' srebra' if sprzedaz > 0 else 'brak ofert'}\n"
            f"Aktualizacja kupna: {data_kupna}\n"
            f"Aktualizacja sprzedaży: {data_sprzedazy}"
        ).replace(",", " ")

        embed.add_field(
            name=f"Jakość: {nazwy_jakosci.get(jakosc, jakosc)}",
            value=wartosc,
            inline=False,
        )

    if aktywne_oferty == 0:
        await interaction.followup.send(
            f"API nie ma aktywnych cen dla **{item_id}** w **{miasto}** na serwerze **{serwer.upper()}**. "
            "To nie musi oznaczać błędu bota — ceny zależą od danych przesłanych do Albion Data Project."
        )
        return

    embed.set_footer(text="Źródło: Albion Data Project • Ceny zależą od dostępności danych rynkowych.")
    await interaction.followup.send(embed=embed)


@albion.command(name="saldo", description="Pokaż swoje prywatne saldo srebra z rozliczeń contentów.")
@app_commands.guild_only()
async def albion_saldo(interaction: discord.Interaction):
    user_id = interaction.user.id
    konto = albion_konta.get(klucz_konta(interaction.guild.id, user_id), {})
    saldo = saldo_konta(interaction.guild.id, user_id)
    zarobione = int(konto.get("zarobione", 0))
    wyplacone = int(konto.get("wyplacone", 0))
    await interaction.response.send_message(
        f"💰 Twoje saldo: **{saldo:,} srebra**\n"
        f"Zarobione w contentach: {zarobione:,} • przekazane: {wyplacone:,}".replace(",", " "),
        ephemeral=True,
    )


@albion.command(name="powiadom-odbior", description="Wyślij DM graczom z dodatnim saldem z informacją o odbiorze silvera.")
@app_commands.guild_only()
@app_commands.check(sprawdz_rozliczajacego)
@app_commands.describe(od="Godzina rozpoczęcia odbioru, np. 18:00.", do="Godzina zakończenia odbioru, np. 20:00.", lokalizacja="Miejsce odbioru silvera, np. Martlock – bank.")
async def albion_powiadom_odbior(
    interaction: discord.Interaction,
    od: str,
    do: str,
    lokalizacja: str,
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    prefix = f"{interaction.guild.id}:"
    adresaci = []
    for klucz in albion_konta:
        if not klucz.startswith(prefix):
            continue
        try:
            user_id = int(klucz.split(":", 1)[1])
        except (TypeError, ValueError):
            continue
        if saldo_konta(interaction.guild.id, user_id) > 0:
            adresaci.append((user_id, saldo_konta(interaction.guild.id, user_id)))

    wyslane = 0
    pominiete = 0
    for user_id, saldo in adresaci:
        czlonek = interaction.guild.get_member(user_id)
        if czlonek is None:
            try:
                czlonek = await interaction.guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pominiete += 1
                continue
        try:
            await czlonek.send(
                f"Masz do odebrania **{saldo:,} srebra** za contenty w Albion Online.\n"
                f"🕒 Odbiór: **{od}–{do}**\n📍 Lokalizacja: **{lokalizacja}**".replace(",", " ")
            )
            wyslane += 1
        except (discord.Forbidden, discord.HTTPException):
            pominiete += 1

    await interaction.followup.send(
        f"Wysłano powiadomienia DM: **{wyslane}**. Nie udało się wysłać lub pobrać graczy: **{pominiete}**.",
        ephemeral=True,
    )


async def autouzupełnij_szablony_rol(interaction: discord.Interaction, wpisywana: str):
    szablony = albion_szablony_rol.get(str(interaction.guild_id), {})
    return [
        app_commands.Choice(name=nazwa[:100], value=nazwa)
        for nazwa in szablony
        if wpisywana.casefold() in nazwa.casefold()
    ][:25]


# ---------- Contenty i rozliczenia ----------

@bot.tree.command(name="content-dodaj", description="Utwórz content; resztę obsłużysz przyciskami na karcie.")
@app_commands.guild_only()
@app_commands.autocomplete(szablon_rol=autouzupełnij_szablony_rol)
@app_commands.describe(
    nazwa="Krótka nazwa wydarzenia, np. Ava 8.3 albo ZvZ.",
    termin="Data i godzina czytelna dla ekipy, np. dziś 20:00.",
    typ="Rodzaj wyprawy, np. Ava, static, gank albo ZvZ.",
    wymagane_ip="Minimalne IP uczestnika, np. 1400. Zostaw puste, jeśli brak.",
    limit_miejsc="Łączna liczba miejsc; role zapełnią resztę jako Pozostałe.",
    opis="Dodatkowe informacje, np. miejsce zbiórki lub wymagany sprzęt.",
    podzial_rol="Wpisz role i liczbę miejsc: Tank:1, Healer:1, DPS:5.",
    szablon_rol="Wybierz zapisany układ ról z podpowiedzi.",
)
async def albion_content_dodaj(
    interaction: discord.Interaction,
    nazwa: str,
    termin: str,
    typ: str,
    wymagane_ip: str = "",
    limit_miejsc: app_commands.Range[int, 1, 100] | None = None,
    opis: str = "",
    podzial_rol: str = "",
    szablon_rol: str = "",
):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("Tej komendy można użyć tylko na serwerze.", ephemeral=True)
        return

    if podzial_rol.strip() and szablon_rol.strip():
        await interaction.response.send_message("Wybierz podział ról albo zapisany szablon — nie oba naraz.", ephemeral=True)
        return
    if szablon_rol.strip():
        role_slots = albion_szablony_rol.get(str(interaction.guild.id), {}).get(szablon_rol)
        if not role_slots:
            await interaction.response.send_message("Nie znaleziono tego szablonu ról na serwerze.", ephemeral=True)
            return
        role_slots = role_slots.copy()
    else:
        try:
            role_slots = parsuj_podzial_rol(podzial_rol)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
    if not role_slots:
        role_slots = {"Bez roli": limit_miejsc or 100}
    suma_miejsc = sum(role_slots.values())
    if limit_miejsc is not None and suma_miejsc > limit_miejsc:
        await interaction.response.send_message("Suma miejsc w rolach przekracza ustawiony limit miejsc.", ephemeral=True)
        return
    if limit_miejsc is not None and suma_miejsc < limit_miejsc:
        if len(role_slots) >= 25:
            await interaction.response.send_message("Wybierz maksymalnie 24 role, aby dodać kategorię Pozostałe.", ephemeral=True)
            return
        pozostale = limit_miejsc - suma_miejsc
        nazwa_pozostalych = "Pozostałe"
        while nazwa_pozostalych in role_slots:
            nazwa_pozostalych += "*"
        role_slots[nazwa_pozostalych] = pozostale
        suma_miejsc = limit_miejsc
    if suma_miejsc > 100:
        await interaction.response.send_message("Łączna liczba miejsc w rolach nie może przekraczać 100.", ephemeral=True)
        return

    content_id = str(interaction.id)
    content = {
        "id": content_id,
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel.id,
        "nazwa": nazwa,
        "termin": termin,
        "typ": typ,
        "ip": wymagane_ip,
        "limit": suma_miejsc,
        "role_slots": role_slots,
        "zapisy_rol": {nazwa: [] for nazwa in role_slots},
        "opis": opis,
        "organizator_id": interaction.user.id,
        "uczestnicy": [],
        "rozliczenie": None,
        "rozliczenie_gildii": False,
        "odwolany": False,
    }
    albion_contenty[content_id] = content
    zapisz_contenty()
    await interaction.response.send_message(
        embed=zbuduj_embed_contentu(content),
        view=ContentView(content_id),
    )
    message = await interaction.original_response()
    content["message_id"] = message.id
    zapisz_contenty()
    try:
        thread = await message.create_thread(
            name=f"Dyskusja: {nazwa}"[:100],
            auto_archive_duration=1440,
        )
        content["thread_id"] = thread.id
        zapisz_contenty()
        await message.edit(embed=zbuduj_embed_contentu(content), view=ContentView(content_id))
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"Nie udało się utworzyć wątku dla contentu {content_id}: {error}")

    try:
        await opublikuj_nowy_panel_finansowy(interaction.guild, interaction.channel)
    except (discord.Forbidden, discord.HTTPException, OSError) as error:
        print(f"Nie udało się odświeżyć panelu rozliczeń po contencie {content_id}: {error}")


@bot.tree.command(name="panel-admin", description="Publikuje lub odświeża panel administratora Albion.")
@app_commands.guild_only()
@wlasciciel_lub_uprawnienie("administrator")
async def panel_admin(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("Użyj tej komendy na serwerze i w kanale tekstowym.", ephemeral=True)
        return
    if not (czy_wlasciciel(interaction.user) or interaction.user.guild_permissions.administrator):
        await interaction.response.send_message("Panel administratora może opublikować tylko administrator serwera.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        wiadomosc = await zapewnij_panel_finansowy(interaction.guild, interaction.channel)
    except discord.HTTPException as error:
        print(f"Nie udało się utworzyć panelu administratora: {error}")
        await interaction.followup.send("Nie udało się opublikować panelu. Sprawdź, czy bot może wysyłać wiadomości i używać przycisków w tym kanale.", ephemeral=True)
        return
    await interaction.followup.send(f"Panel administratora jest gotowy: {wiadomosc.jump_url}", ephemeral=True)


# ---------- Grupa /wot: World of Tanks PC EU ----------

class WotAPIError(RuntimeError):
    pass


WOT_VEHICLES_CACHE = []
WOT_VEHICLES_CACHE_AT = 0
WOT_VEHICLES_CACHE_TTL = 6 * 60 * 60


async def wot_api_get(endpoint, params=None):
    if not WOT_APPLICATION_ID:
        raise WotAPIError("Brak WOT_APPLICATION_ID. Dodaj ID aplikacji Wargaming do konfiguracji hostingu.")

    request_params = {
        "application_id": WOT_APPLICATION_ID,
        "language": "pl",
    }
    request_params.update(params or {})
    url = f"{WOT_API_ROOT}/{endpoint.strip('/')}/"
    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=request_params) as response:
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    raise WotAPIError(f"API Wargaming zwróciło nieczytelną odpowiedź (HTTP {response.status}).")
                if response.status != 200:
                    raise WotAPIError(f"API Wargaming zwróciło HTTP {response.status}.")
    except WotAPIError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise WotAPIError(f"Nie udało się połączyć z API Wargaming: {error!r}") from error

    if payload.get("status") != "ok":
        error = payload.get("error") or {}
        message = error.get("message") or "API Wargaming odrzuciło zapytanie."
        code = error.get("code")
        raise WotAPIError(f"{message}" + (f" (kod: {code})" if code else ""))
    return payload.get("data")


def wot_wartosc_z_mapy(data, identyfikator=None):
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        if identyfikator is not None:
            return data.get(str(identyfikator), data.get(identyfikator))
        return next(iter(data.values()), None)
    return None


def wot_lista(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.values())
    return []


async def wot_znajdz_gracza(nick):
    wyniki = await wot_api_get("account/list", {
        "search": nick.strip(),
        "type": "exact",
        "limit": 10,
        "fields": "account_id,nickname",
    })
    gracze = wot_lista(wyniki)
    dokladni = [gracz for gracz in gracze if str(gracz.get("nickname", "")).casefold() == nick.strip().casefold()]
    if not dokladni:
        raise WotAPIError(f"Nie znaleziono gracza o nicku **{nick[:60]}** na serwerze EU.")
    return dokladni[0]


async def wot_pobierz_profil(nick):
    gracz = await wot_znajdz_gracza(nick)
    account_id = gracz.get("account_id")
    dane = await wot_api_get("account/info", {
        "account_id": account_id,
        "fields": "nickname,created_at,last_battle_time,clan_id,global_rating,statistics.all,statistics.random",
    })
    profil = wot_wartosc_z_mapy(dane, account_id)
    if not profil:
        raise WotAPIError(f"Nie udało się pobrać publicznych statystyk gracza {nick}.")
    return gracz, profil


def wot_statystyki(profil):
    statystyki = profil.get("statistics") or {}
    return statystyki.get("all") or statystyki.get("random") or {}


def wot_procent_wygranych(statystyki):
    bitwy = int(statystyki.get("battles") or 0)
    wygrane = int(statystyki.get("wins") or 0)
    return (wygrane / bitwy * 100) if bitwy else 0.0


def wot_formatuj_liczbe(wartosc):
    try:
        return f"{int(wartosc):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def wot_formatuj_date(timestamp):
    try:
        if not timestamp:
            return "Brak danych"
        return datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%d.%m.%Y")
    except (TypeError, ValueError, OSError, OverflowError):
        return "Brak danych"


def wot_embed_gracza(nick, profil):
    statystyki = wot_statystyki(profil)
    bitwy = int(statystyki.get("battles") or 0)
    wygrane = int(statystyki.get("wins") or 0)
    procent = wot_procent_wygranych(statystyki)
    srednie_uszkodzenia = (int(statystyki.get("damage_dealt") or 0) / bitwy) if bitwy else 0
    pasek = "🟩" * min(10, max(0, int(procent // 10))) + "⬛" * (10 - min(10, max(0, int(procent // 10))))
    embed = discord.Embed(
        title=f"🎖️ {profil.get('nickname') or nick}",
        description=f"**World of Tanks PC · Europa**\n{pasek} **{procent:.2f}% WR**",
        color=discord.Color.gold(),
    )
    embed.add_field(name="⚔️ Bitwy", value=wot_formatuj_liczbe(bitwy), inline=True)
    embed.add_field(name="🏆 Zwycięstwa", value=f"{wot_formatuj_liczbe(wygrane)} · {procent:.2f}%", inline=True)
    embed.add_field(name="💥 Śr. uszkodzenia", value=f"{srednie_uszkodzenia:,.0f}".replace(",", " "), inline=True)
    embed.add_field(name="☠️ Zniszczone pojazdy", value=wot_formatuj_liczbe(statystyki.get("frags")), inline=True)
    embed.add_field(name="🔥 Rekord uszkodzeń", value=wot_formatuj_liczbe(statystyki.get("max_damage")), inline=True)
    embed.add_field(name="⭐ Global rating", value=wot_formatuj_liczbe(profil.get("global_rating")), inline=True)
    embed.add_field(name="📅 Konto od", value=wot_formatuj_date(profil.get("created_at")), inline=True)
    embed.add_field(name="🛡️ Klan", value=f"ID {profil['clan_id']}" if profil.get("clan_id") else "Bez klanu", inline=True)
    embed.set_footer(text="Źródło: Wargaming Public API · WoT EU")
    return embed


async def wot_pobierz_katalog_pojazdow(force=False):
    global WOT_VEHICLES_CACHE, WOT_VEHICLES_CACHE_AT
    teraz = datetime.now(timezone.utc).timestamp()
    if WOT_VEHICLES_CACHE and not force and teraz - WOT_VEHICLES_CACHE_AT < WOT_VEHICLES_CACHE_TTL:
        return WOT_VEHICLES_CACHE
    dane = await wot_api_get("encyclopedia/vehicles", {
        "fields": "tank_id,name,tier,nation,type,is_premium,description",
        "limit": 1000,
    })
    WOT_VEHICLES_CACHE = wot_lista(dane)
    WOT_VEHICLES_CACHE_AT = teraz
    return WOT_VEHICLES_CACHE


async def wot_pobierz_pojazdy_gracza(account_id):
    dane = await wot_api_get("tanks/stats", {
        "account_id": account_id,
        "fields": "tank_id,mark_of_mastery,all.battles,all.wins,all.damage_dealt,all.frags,all.max_damage",
        "limit": 1000,
    })
    statystyki = wot_lista(dane)
    if not statystyki:
        raise WotAPIError("API nie zwróciło pojazdów. Profil może być prywatny albo konto nie ma rozegranych bitew.")
    statystyki.sort(key=lambda wpis: int((wpis.get("all") or {}).get("battles") or 0), reverse=True)
    wybrane = statystyki[:10]
    ids = ",".join(str(wpis["tank_id"]) for wpis in wybrane if wpis.get("tank_id") is not None)
    katalog = await wot_api_get("encyclopedia/vehicles", {
        "tank_id": ids,
        "fields": "tank_id,name,tier,nation,type",
    }) if ids else {}
    mapa = katalog if isinstance(katalog, dict) else {
        str(wpis.get("tank_id")): wpis for wpis in katalog if isinstance(wpis, dict)
    }
    return wybrane, mapa


async def wot_pobierz_klan(query):
    szukana = query.strip()
    wyniki = await wot_api_get("clans/list", {
        "search": szukana,
        "limit": 25,
        "fields": "clan_id,name,tag,members_count,created_at",
    })
    klany = wot_lista(wyniki)
    if not klany and szukana.startswith("[") and szukana.endswith("]"):
        wyniki = await wot_api_get("clans/list", {
            "search": szukana[1:-1],
            "limit": 25,
            "fields": "clan_id,name,tag,members_count,created_at",
        })
        klany = wot_lista(wyniki)
    dokladne = [
        klan for klan in klany
        if str(klan.get("tag", "")).casefold() == szukana.strip("[]").casefold()
        or str(klan.get("name", "")).casefold() == szukana.casefold()
    ]
    if len(dokladne) == 1:
        wybrany = dokladne[0]
    elif len(klany) == 1:
        wybrany = klany[0]
    elif klany:
        propozycje = ", ".join(f"[{k.get('tag', '?')}] {k.get('name', '')}" for k in klany[:8])
        raise WotAPIError(f"Znaleziono kilka klanów. Podaj dokładniejszą nazwę lub tag: {propozycje}")
    else:
        raise WotAPIError(f"Nie znaleziono klanu **{szukana[:60]}** na serwerze EU.")

    szczegoly = await wot_api_get("clans/info", {
        "clan_id": wybrany["clan_id"],
        "fields": "clan_id,name,tag,description,created_at,members_count,members.account_name,members.role_i18n,members.joined_at",
    })
    info = wot_wartosc_z_mapy(szczegoly, wybrany["clan_id"])
    if not info:
        raise WotAPIError("Nie udało się pobrać szczegółów klanu.")
    return info


async def wot_sprawdz_konfiguracje(interaction):
    if WOT_APPLICATION_ID:
        return True
    await interaction.response.send_message(
        "Komendy WoT wymagają `WOT_APPLICATION_ID`. Zarejestruj aplikację w Wargaming Developer Room i ustaw jej Application ID w konfiguracji hostingu.",
        ephemeral=True,
    )
    return False


@wot.command(name="gracz", description="Pokaż publiczne statystyki gracza WoT na EU.")
@app_commands.describe(nick="Dokładny nick gracza World of Tanks PC.")
async def wot_gracz(interaction: discord.Interaction, nick: str):
    if not await wot_sprawdz_konfiguracje(interaction):
        return
    await interaction.response.defer(thinking=True)
    try:
        _, profil = await wot_pobierz_profil(nick)
        await interaction.followup.send(embed=wot_embed_gracza(nick, profil))
    except WotAPIError as error:
        await interaction.followup.send(f"Nie udało się pobrać profilu WoT: {error}", ephemeral=True)


@wot.command(name="porownaj", description="Porównaj statystyki dwóch graczy WoT na EU.")
@app_commands.describe(nick1="Pierwszy nick gracza.", nick2="Drugi nick gracza.")
async def wot_porownaj(interaction: discord.Interaction, nick1: str, nick2: str):
    if not await wot_sprawdz_konfiguracje(interaction):
        return
    await interaction.response.defer(thinking=True)
    try:
        (_, profil1), (_, profil2) = await asyncio.gather(
            wot_pobierz_profil(nick1),
            wot_pobierz_profil(nick2),
        )
        stat1 = wot_statystyki(profil1)
        stat2 = wot_statystyki(profil2)
        bitwy1, bitwy2 = int(stat1.get("battles") or 0), int(stat2.get("battles") or 0)
        avg1 = int(stat1.get("damage_dealt") or 0) / bitwy1 if bitwy1 else 0
        avg2 = int(stat2.get("damage_dealt") or 0) / bitwy2 if bitwy2 else 0
        embed = discord.Embed(
            title="⚔️ Porównanie graczy WoT EU",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name=f"🎖️ {profil1.get('nickname') or nick1}",
            value=(
                f"Bitwy: **{wot_formatuj_liczbe(bitwy1)}**\n"
                f"WR: **{wot_procent_wygranych(stat1):.2f}%**\n"
                f"Śr. uszkodzenia: **{avg1:,.0f}**\n"
                f"Zniszczenia: **{wot_formatuj_liczbe(stat1.get('frags'))}**"
            ).replace(",", " "),
            inline=True,
        )
        embed.add_field(
            name=f"🎖️ {profil2.get('nickname') or nick2}",
            value=(
                f"Bitwy: **{wot_formatuj_liczbe(bitwy2)}**\n"
                f"WR: **{wot_procent_wygranych(stat2):.2f}%**\n"
                f"Śr. uszkodzenia: **{avg2:,.0f}**\n"
                f"Zniszczenia: **{wot_formatuj_liczbe(stat2.get('frags'))}**"
            ).replace(",", " "),
            inline=True,
        )
        embed.set_footer(text="World of Tanks PC · Europa · Wargaming Public API")
        await interaction.followup.send(embed=embed)
    except WotAPIError as error:
        await interaction.followup.send(f"Nie udało się porównać graczy: {error}", ephemeral=True)


@wot.command(name="pojazdy", description="Pokaż 10 najczęściej używanych czołgów gracza.")
@app_commands.describe(nick="Dokładny nick gracza World of Tanks PC.")
async def wot_pojazdy(interaction: discord.Interaction, nick: str):
    if not await wot_sprawdz_konfiguracje(interaction):
        return
    await interaction.response.defer(thinking=True)
    try:
        gracz = await wot_znajdz_gracza(nick)
        pojazdy, mapa = await wot_pobierz_pojazdy_gracza(gracz["account_id"])
        embed = discord.Embed(
            title=f"🚜 Najczęściej używane czołgi · {gracz.get('nickname', nick)}",
            description="Ranking według liczby bitew.",
            color=discord.Color.gold(),
        )
        for miejsce, wpis in enumerate(pojazdy[:10], start=1):
            tank_id = str(wpis.get("tank_id"))
            pojazd = mapa.get(tank_id, mapa.get(wpis.get("tank_id"), {})) or {}
            dane = wpis.get("all") or {}
            bitwy = int(dane.get("battles") or 0)
            wygrane = int(dane.get("wins") or 0)
            wr = wygrane / bitwy * 100 if bitwy else 0
            nazwa = pojazd.get("name") or f"Pojazd ID {tank_id}"
            tier = pojazd.get("tier")
            tytul = f"{miejsce}. {nazwa}" + (f" · VIII poziom {tier}" if tier else "")
            embed.add_field(
                name=tytul[:256],
                value=f"⚔️ {wot_formatuj_liczbe(bitwy)} bitew · 🏆 {wr:.1f}% WR · 💥 {int(dane.get('damage_dealt') or 0) // bitwy if bitwy else 0} śr. uszkodzeń",
                inline=False,
            )
        embed.set_footer(text="Źródło: Wargaming Public API · WoT EU")
        await interaction.followup.send(embed=embed)
    except WotAPIError as error:
        await interaction.followup.send(f"Nie udało się pobrać pojazdów: {error}", ephemeral=True)


@wot.command(name="czolg", description="Wyszukaj czołg w encyklopedii WoT.")
@app_commands.describe(nazwa="Nazwa czołgu; może być pełna albo fragment.")
async def wot_czolg(interaction: discord.Interaction, nazwa: str):
    if not await wot_sprawdz_konfiguracje(interaction):
        return
    await interaction.response.defer(thinking=True)
    try:
        katalog = await wot_pobierz_katalog_pojazdow()
        pasujace = [
            pojazd for pojazd in katalog
            if nazwa.strip().casefold() in str(pojazd.get("name", "")).casefold()
        ]
        if not pasujace:
            await interaction.followup.send(f"Nie znaleziono czołgu **{nazwa[:80]}** w encyklopedii WoT EU.", ephemeral=True)
            return
        pasujace.sort(key=lambda pojazd: (str(pojazd.get("name", "")).casefold() != nazwa.strip().casefold(), pojazd.get("tier", 0)))
        if len(pasujace) > 1 and str(pasujace[0].get("name", "")).casefold() != nazwa.strip().casefold():
            lista = "\n".join(
                f"• **{pojazd.get('name')}** · poziom {pojazd.get('tier')} · {pojazd.get('nation')}"
                for pojazd in pasujace[:12]
            )
            await interaction.followup.send(f"Znalazłem kilka czołgów pasujących do nazwy. Doprecyzuj zapytanie:\n{lista}", ephemeral=True)
            return
        pojazd = pasujace[0]
        szczegoly = await wot_api_get("encyclopedia/vehicles", {
            "tank_id": pojazd.get("tank_id"),
            "fields": "tank_id,name,tier,nation,type,is_premium,description,default_profile",
        })
        pojazd = wot_wartosc_z_mapy(szczegoly, pojazd.get("tank_id")) or pojazd
        default = pojazd.get("default_profile") or {}
        armor = default.get("armor") or {}
        gun = default.get("gun") or {}
        typy = {
            "lightTank": "Lekki",
            "mediumTank": "Średni",
            "heavyTank": "Ciężki",
            "AT-SPG": "Niszczyciel czołgów",
            "SPG": "Artyleria",
        }
        embed = discord.Embed(
            title=f"🛡️ {pojazd.get('name', nazwa)}",
            description=(
                f"**Poziom {pojazd.get('tier', '?')}** · {typy.get(pojazd.get('type'), pojazd.get('type', 'Pojazd'))} · "
                f"{pojazd.get('nation', 'Nieznana nacja')}"
                + (" · ⭐ Premium" if pojazd.get("is_premium") else "")
            ),
            color=discord.Color.gold(),
        )
        if default.get("hp") is not None:
            embed.add_field(name="❤️ Wytrzymałość", value=wot_formatuj_liczbe(default.get("hp")), inline=True)
        if default.get("speed_forward") is not None:
            embed.add_field(name="💨 Prędkość", value=f"{default.get('speed_forward')} km/h", inline=True)
        if armor:
            przod = armor.get("hull", {}).get("front") if isinstance(armor.get("hull"), dict) else None
            wieza = armor.get("turret", {}).get("front") if isinstance(armor.get("turret"), dict) else None
            if przod is not None or wieza is not None:
                embed.add_field(name="🧱 Pancerz przód", value=f"Kadłub: {przod or '—'} mm\nWieża: {wieza or '—'} mm", inline=True)
        obrazenia = gun.get("damage") if isinstance(gun, dict) else None
        penetracja = gun.get("penetration") if isinstance(gun, dict) else None
        if obrazenia:
            embed.add_field(name="💥 Uszkodzenia działa", value=" / ".join(str(w) for w in obrazenia[:3]) + " HP", inline=True)
        if penetracja:
            embed.add_field(name="🎯 Penetracja", value=" / ".join(str(w) for w in penetracja[:3]) + " mm", inline=True)
        opis = str(pojazd.get("description") or "").strip()
        if opis:
            embed.add_field(name="Opis", value=opis[:1000], inline=False)
        embed.set_footer(text=f"ID pojazdu: {pojazd.get('tank_id')} · Wargaming Public API · WoT EU")
        await interaction.followup.send(embed=embed)
    except WotAPIError as error:
        await interaction.followup.send(f"Nie udało się pobrać danych czołgu: {error}", ephemeral=True)


@wot.command(name="klan", description="Pokaż informacje o klanie i jego członkach.")
@app_commands.describe(tag_lub_nazwa="Tag, np. [ABC], albo nazwa klanu.")
async def wot_klan(interaction: discord.Interaction, tag_lub_nazwa: str):
    if not await wot_sprawdz_konfiguracje(interaction):
        return
    await interaction.response.defer(thinking=True)
    try:
        klan = await wot_pobierz_klan(tag_lub_nazwa)
        embed = discord.Embed(
            title=f"🛡️ [{klan.get('tag', '?')}] {klan.get('name', 'Klan WoT')}",
            description=str(klan.get("description") or "Brak opisu klanu.")[:1000],
            color=discord.Color.dark_gold(),
        )
        embed.add_field(name="👥 Członkowie", value=wot_formatuj_liczbe(klan.get("members_count")), inline=True)
        embed.add_field(name="📅 Założony", value=wot_formatuj_date(klan.get("created_at")), inline=True)
        czlonkowie = klan.get("members") or []
        if isinstance(czlonkowie, dict):
            czlonkowie = list(czlonkowie.values())
        if czlonkowie:
            top = sorted(czlonkowie, key=lambda osoba: (str(osoba.get("role_i18n") or "").casefold(), str(osoba.get("account_name") or "").casefold()))[:15]
            linie = [
                f"• **{osoba.get('account_name', 'Nieznany')}** — {osoba.get('role_i18n') or 'Członek'}"
                for osoba in top
            ]
            embed.add_field(name="📜 Skład klanu", value="\n".join(linie)[:1024], inline=False)
        embed.set_footer(text=f"ID klanu: {klan.get('clan_id')} · Wargaming Public API · WoT EU")
        await interaction.followup.send(embed=embed)
    except WotAPIError as error:
        await interaction.followup.send(f"Nie udało się pobrać klanu: {error}", ephemeral=True)


# ---------- Start ----------

token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("Brak zmiennej środowiskowej DISCORD_TOKEN.")

bot.run(token)
