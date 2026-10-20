import html
import os
import re
import sqlite3
import time
from typing import Optional
from urllib.parse import quote_plus
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.getenv("DISCORD_TOKEN", "COLE_SEU_TOKEN_AQUI")
ANILIST = "https://graphql.anilist.co"
DB = "animes.db"

# Plataformas que você assina. São padrões (regex) comparados com o nome do site no AniList,
# em minúsculas. Para adicionar outra, inclua um padrão novo na lista.
PLATAFORMAS = [
    r"crunchyroll",
    r"netflix",
    r"disney",          # Disney Plus / Disney+
    r"prime video",     # Amazon Prime Video
    r"amazon",
    r"hbo",             # HBO Max
    r"\bmax\b",         # Max (com borda de palavra, para não pegar "Animax")
]
# Quanto tempo antes do episódio mandar o lembrete (segundos)
LEMBRETE_SEG = 3600

db = sqlite3.connect(DB)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS follows(
        user_id INTEGER, media_id INTEGER, title TEXT,
        PRIMARY KEY(user_id, media_id));
    CREATE TABLE IF NOT EXISTS channels(user_id INTEGER PRIMARY KEY, channel_id INTEGER);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v INTEGER);
    """
)
db.commit()


def get_last_check() -> int:
    row = db.execute("SELECT v FROM meta WHERE k='last_check'").fetchone()
    return row[0] if row else int(time.time())


def set_last_check(ts: int):
    db.execute("INSERT OR REPLACE INTO meta VALUES('last_check', ?)", (ts,))
    db.commit()


async def anilist(session: aiohttp.ClientSession, query: str, variables: dict):
    async with session.post(ANILIST, json={"query": query, "variables": variables}) as r:
        r.raise_for_status()
        return (await r.json())["data"]


def nome(t: dict) -> str:
    return t.get("english") or t.get("romaji") or "?"


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        set_last_check(get_last_check())
        checar.start()


bot = Bot()


async def autocomplete(_: discord.Interaction, atual: str):
    if len(atual) < 2:
        return []
    q = """query($s:String){Page(perPage:10){media(search:$s,type:ANIME,sort:POPULARITY_DESC){
        id title{romaji english}}}}"""
    try:
        data = await anilist(bot.session, q, {"s": atual})
    except Exception:
        return []
    return [
        app_commands.Choice(name=nome(m["title"])[:100], value=str(m["id"]))
        for m in data["Page"]["media"]
    ]


@bot.tree.command(name="seguir", description="Seguir um anime para receber aviso de episódios")
@app_commands.describe(anime="Nome do anime")
@app_commands.autocomplete(anime=autocomplete)
async def seguir(inter: discord.Interaction, anime: str):
    if not anime.isdigit():
        return await inter.response.send_message("Escolha um item da lista de sugestões.", ephemeral=True)
    q = "query($id:Int){Media(id:$id){id title{romaji english}}}"
    m = (await anilist(bot.session, q, {"id": int(anime)}))["Media"]
    db.execute("INSERT OR REPLACE INTO follows VALUES(?,?,?)", (inter.user.id, m["id"], nome(m["title"])))
    db.commit()
    await inter.response.send_message(f"✅ Agora você segue **{nome(m['title'])}**.", ephemeral=True)


async def seguindo_ac(inter: discord.Interaction, atual: str):
    rows = db.execute("SELECT media_id, title FROM follows WHERE user_id=?", (inter.user.id,)).fetchall()
    return [
        app_commands.Choice(name=t[:100], value=str(i))
        for i, t in rows if atual.lower() in t.lower()
    ][:25]


@bot.tree.command(name="deixar", description="Parar de seguir um anime")
@app_commands.autocomplete(anime=seguindo_ac)
async def deixar(inter: discord.Interaction, anime: str):
    db.execute("DELETE FROM follows WHERE user_id=? AND media_id=?", (inter.user.id, int(anime)))
    db.commit()
    await inter.response.send_message("🗑️ Removido.", ephemeral=True)


@bot.tree.command(name="lista", description="Animes que você segue")
async def lista(inter: discord.Interaction):
    rows = db.execute("SELECT title FROM follows WHERE user_id=?", (inter.user.id,)).fetchall()
    txt = "\n".join(f"• {t}" for (t,) in rows) or "Você ainda não segue nenhum anime."
    await inter.response.send_message(txt, ephemeral=True)


@bot.tree.command(name="proximos", description="Quando sai o próximo episódio de cada anime que você segue")
async def proximos(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    rows = db.execute("SELECT media_id, title FROM follows WHERE user_id=?", (inter.user.id,)).fetchall()
    if not rows:
        return await inter.followup.send("Você ainda não segue nenhum anime.")
    q = "query($id:Int){Media(id:$id){nextAiringEpisode{episode airingAt}}}"
    linhas = []
    for mid, titulo in rows:
        n = (await anilist(bot.session, q, {"id": mid}))["Media"]["nextAiringEpisode"]
        if n:
            linhas.append(f"• **{titulo}** — ep. {n['episode']} em <t:{n['airingAt']}:R> (<t:{n['airingAt']}:f>)")
        else:
            linhas.append(f"• **{titulo}** — sem episódio agendado")
    await inter.followup.send("\n".join(linhas))


@bot.tree.command(name="canal", description="Receber os avisos neste canal em vez de DM")
async def canal(inter: discord.Interaction):
    db.execute("INSERT OR REPLACE INTO channels VALUES(?,?)", (inter.user.id, inter.channel_id))
    db.commit()
    await inter.response.send_message("📍 Os avisos virão para este canal (com menção).", ephemeral=True)


# ---------------------------------------------------------------------------
# Perfil do bot: avatar e banner (somente o dono do bot pode usar)
# ---------------------------------------------------------------------------
TIPOS_OK = ("image/png", "image/jpeg", "image/gif", "image/webp")
LIMITE_MB = 100
LIMITE_BYTES = LIMITE_MB * 1024 * 1024


async def eh_dono(user: discord.abc.User) -> bool:
    info = await bot.application_info()
    if info.team:
        return user.id in {m.id for m in info.team.members}
    return user.id == info.owner.id


async def obter_imagem(arquivo: Optional[discord.Attachment], link: Optional[str]) -> bytes:
    if bool(arquivo) == bool(link):
        raise ValueError("Envie **um** arquivo **ou** um link (nem os dois, nem nenhum).")

    if arquivo:
        tipo = (arquivo.content_type or "").split(";")[0]
        if tipo not in TIPOS_OK:
            raise ValueError("Formato não suportado. Use PNG, JPG, GIF ou WEBP.")
        if arquivo.size > LIMITE_BYTES:
            raise ValueError(f"Arquivo grande demais (máximo {LIMITE_MB} MB).")
        return await arquivo.read()

    if not link.startswith(("http://", "https://")):
        raise ValueError("O link precisa começar com http:// ou https://")
    async with bot.session.get(link) as r:
        if r.status != 200:
            raise ValueError(f"O link respondeu com erro {r.status}.")
        if r.content_type not in TIPOS_OK:
            raise ValueError("O link não aponta para uma imagem PNG, JPG, GIF ou WEBP.")
        dados = bytearray()
        async for pedaco in r.content.iter_chunked(65536):
            dados.extend(pedaco)
            if len(dados) > LIMITE_BYTES:
                raise ValueError(f"Imagem grande demais (máximo {LIMITE_MB} MB).")
        return bytes(dados)


async def atualizar_perfil(inter: discord.Interaction, campo: str, rotulo: str,
                           arquivo: Optional[discord.Attachment], link: Optional[str]):
    if not await eh_dono(inter.user):
        return await inter.response.send_message("🚫 Só o dono do bot pode usar este comando.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    try:
        dados = await obter_imagem(arquivo, link)
        await bot.user.edit(**{campo: dados})
    except ValueError as e:
        return await inter.followup.send(f"⚠️ {e}")
    except TypeError:
        return await inter.followup.send("⚠️ Sua versão do discord.py não suporta isso. Atualize: `pip install -U discord.py`")
    except discord.HTTPException as e:
        return await inter.followup.send(f"⚠️ O Discord recusou a imagem: {e}")
    await inter.followup.send(f"✅ {rotulo} atualizado!")


@bot.tree.command(name="avatar", description="Define o avatar do bot (arquivo OU link, foto ou GIF)")
@app_commands.describe(arquivo="Imagem/GIF enviado do seu aparelho", link="Link direto para a imagem/GIF")
async def avatar(inter: discord.Interaction, arquivo: Optional[discord.Attachment] = None, link: Optional[str] = None):
    await atualizar_perfil(inter, "avatar", "Avatar", arquivo, link)


@bot.tree.command(name="banner", description="Define o banner (faixa do perfil) do bot (arquivo OU link)")
@app_commands.describe(arquivo="Imagem/GIF enviado do seu aparelho", link="Link direto para a imagem/GIF")
async def banner(inter: discord.Interaction, arquivo: Optional[discord.Attachment] = None, link: Optional[str] = None):
    await atualizar_perfil(inter, "banner", "Banner", arquivo, link)


# ---------------------------------------------------------------------------
# /manga: ficha do mangá + botões para plataformas oficiais/legais
# ---------------------------------------------------------------------------
STATUS_PT = {
    "FINISHED": "Finalizado",
    "RELEASING": "Em publicação",
    "NOT_YET_RELEASED": "Ainda não lançado",
    "CANCELLED": "Cancelado",
    "HIATUS": "Em hiato",
}


def limpar_html(txt: Optional[str], limite: int = 450) -> str:
    if not txt:
        return "Sem sinopse disponível."
    txt = re.sub(r"<br\s*/?>", "\n", txt)
    txt = html.unescape(re.sub(r"<[^>]+>", "", txt)).strip()
    return txt if len(txt) <= limite else txt[:limite].rsplit(" ", 1)[0] + "…"


async def manga_autocomplete(_: discord.Interaction, atual: str):
    if len(atual) < 2:
        return []
    q = """query($s:String){Page(perPage:10){media(search:$s,type:MANGA,sort:POPULARITY_DESC){
        id title{romaji english}}}}"""
    try:
        data = await anilist(bot.session, q, {"s": atual})
    except Exception:
        return []
    return [
        app_commands.Choice(name=nome(m["title"])[:100], value=str(m["id"]))
        for m in data["Page"]["media"]
    ]


@bot.tree.command(name="manga", description="Ficha de um mangá e onde ler legalmente")
@app_commands.describe(titulo="Nome do mangá")
@app_commands.autocomplete(titulo=manga_autocomplete)
async def manga(inter: discord.Interaction, titulo: str):
    if not titulo.isdigit():
        return await inter.response.send_message("Escolha um item da lista de sugestões.", ephemeral=True)
    await inter.response.defer()

    q = """query($id:Int){Media(id:$id,type:MANGA){
        title{romaji english} description status chapters volumes genres averageScore
        siteUrl coverImage{large} externalLinks{site url type}}}"""
    m = (await anilist(bot.session, q, {"id": int(titulo)}))["Media"]
    nm = nome(m["title"])

    emb = discord.Embed(title=nm, url=m["siteUrl"], description=limpar_html(m["description"]), color=0x3F51B5)
    emb.set_thumbnail(url=m["coverImage"]["large"])
    emb.add_field(name="Status", value=STATUS_PT.get(m["status"], m["status"] or "?"), inline=True)
    emb.add_field(name="Capítulos", value=str(m["chapters"] or "—"), inline=True)
    emb.add_field(name="Volumes", value=str(m["volumes"] or "—"), inline=True)
    if m["averageScore"]:
        emb.add_field(name="Nota", value=f"{m['averageScore']}/100", inline=True)
    if m["genres"]:
        emb.add_field(name="Gêneros", value=", ".join(m["genres"][:6]), inline=False)
    emb.set_footer(text="Dados: AniList • A sinopse costuma estar em inglês")

    view = discord.ui.View()
    oficiais = [l for l in m["externalLinks"] if l["type"] == "STREAMING"][:3]
    for l in oficiais:
        view.add_item(discord.ui.Button(label=f"Ler na {l['site']}", url=l["url"]))
    busca = "https://www.google.com/search?q=" + quote_plus(f"{nm} mangá edição brasileira")
    view.add_item(discord.ui.Button(label="Edição no Brasil", url=busca))
    view.add_item(discord.ui.Button(label="AniList", url=m["siteUrl"]))

    await inter.followup.send(embed=emb, view=view)


def links_de_streaming(media: dict) -> list[dict]:
    """Prefere as plataformas que você assina; se não houver, mostra as demais legais."""
    todos = [l for l in media["externalLinks"] if l["type"] == "STREAMING"]
    meus = [l for l in todos if any(re.search(p, l["site"].lower()) for p in PLATAFORMAS)]
    return (meus or todos)[:5]


async def buscar_episodios(ids: list[int], a: int, b: int) -> list[dict]:
    q = """query($ids:[Int],$a:Int,$b:Int){Page(perPage:50){
        airingSchedules(mediaId_in:$ids,airingAt_greater:$a,airingAt_lesser:$b){
            episode airingAt
            media{id title{romaji english} siteUrl coverImage{large}
                  externalLinks{site url type}}}}}"""
    data = await anilist(bot.session, q, {"ids": ids, "a": a, "b": b})
    return data["Page"]["airingSchedules"]


async def enviar(ep: dict, lembrete: bool):
    m = ep["media"]
    links = links_de_streaming(m)
    if lembrete:
        titulo = f"⏰ {nome(m['title'])} — Ep. {ep['episode']} sai em cerca de 1 hora"
        desc = f"Estreia <t:{ep['airingAt']}:R> (às <t:{ep['airingAt']}:t>)."
        cor = 0xFFC107
    else:
        titulo = f"📺 {nome(m['title'])} — Episódio {ep['episode']}"
        desc = "Acabou de ser lançado!"
        cor = 0xE91E63

    emb = discord.Embed(title=titulo, url=m["siteUrl"], description=desc, color=cor)
    emb.set_thumbnail(url=m["coverImage"]["large"])

    kwargs = {"embed": emb}
    if links and not lembrete:
        view = discord.ui.View()
        for l in links:
            view.add_item(discord.ui.Button(label=f"Assistir na {l['site']}", url=l["url"]))
        kwargs["view"] = view

    seguidores = db.execute("SELECT user_id FROM follows WHERE media_id=?", (m["id"],)).fetchall()
    for (uid,) in seguidores:
        ch = db.execute("SELECT channel_id FROM channels WHERE user_id=?", (uid,)).fetchone()
        try:
            if ch and (c := bot.get_channel(ch[0])):
                await c.send(f"<@{uid}>", **kwargs)
            else:
                user = await bot.fetch_user(uid)
                await user.send(**kwargs)
        except discord.HTTPException as e:
            print(f"Falha ao avisar {uid}: {e}")


@tasks.loop(minutes=5)
async def checar():
    agora = int(time.time())
    desde = get_last_check()
    ids = [r[0] for r in db.execute("SELECT DISTINCT media_id FROM follows")]
    if not ids:
        return set_last_check(agora)

    try:
        lancados = await buscar_episodios(ids, desde, agora)
        # mesma janela de tempo, deslocada 1h para frente = episódios que saem daqui a ~1h
        lembretes = await buscar_episodios(ids, desde + LEMBRETE_SEG, agora + LEMBRETE_SEG)
    except Exception as e:
        print("Erro AniList:", e)
        return  # tenta de novo no próximo ciclo sem perder a janela

    for ep in lancados:
        await enviar(ep, lembrete=False)
    for ep in lembretes:
        await enviar(ep, lembrete=True)

    set_last_check(agora)


@checar.before_loop
async def _espera():
    await bot.wait_until_ready()


if __name__ == "__main__":
    bot.run(TOKEN)
