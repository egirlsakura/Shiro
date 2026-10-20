import asyncio
import html
import io
import os
import re
import shutil
import sqlite3
import time
import uuid
import zipfile
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
    CREATE TABLE IF NOT EXISTS biblioteca(
        id INTEGER PRIMARY KEY AUTOINCREMENT, dono_id INTEGER,
        titulo TEXT, caminho TEXT, paginas INTEGER);
    CREATE TABLE IF NOT EXISTS progresso(
        user_id INTEGER, manga_id INTEGER, pagina INTEGER,
        PRIMARY KEY(user_id, manga_id));
    CREATE TABLE IF NOT EXISTS canais(
        user_id INTEGER, channel_id INTEGER, guild_id INTEGER,
        PRIMARY KEY(user_id, channel_id));
    CREATE TABLE IF NOT EXISTS membros(
        dono_id INTEGER, membro_id INTEGER,
        PRIMARY KEY(dono_id, membro_id));
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


class CanaisView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Selecione um ou vários canais",
        min_values=1,
        max_values=25,
    )
    async def escolher(self, inter: discord.Interaction, select: discord.ui.ChannelSelect):
        db.execute("DELETE FROM canais WHERE user_id=? AND guild_id=?", (inter.user.id, inter.guild_id))
        linhas = []
        for c in select.values:
            db.execute("INSERT OR REPLACE INTO canais VALUES(?,?,?)", (inter.user.id, c.id, inter.guild_id))
            real = inter.guild.get_channel(c.id)
            faltando = []
            if real:
                p = real.permissions_for(inter.guild.me)
                for rotulo, ok in (("Ver canais", p.view_channel),
                                   ("Enviar mensagens", p.send_messages),
                                   ("Incorporar links", p.embed_links)):
                    if not ok:
                        faltando.append(rotulo)
            if faltando:
                linhas.append(f"⚠️ <#{c.id}>: o bot não tem a permissão {', '.join(faltando)}")
            else:
                linhas.append(f"✅ <#{c.id}>")
        db.commit()
        await inter.response.edit_message(
            content="📍 **Canais de aviso deste servidor atualizados:**\n" + "\n".join(linhas), view=None)

    @discord.ui.button(label="Remover todos os canais deste servidor", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def limpar(self, inter: discord.Interaction, button: discord.ui.Button):
        db.execute("DELETE FROM canais WHERE user_id=? AND guild_id=?", (inter.user.id, inter.guild_id))
        db.commit()
        await inter.response.edit_message(content="🗑️ Nenhum canal deste servidor recebe mais os seus avisos.", view=None)


@bot.tree.command(name="canal", description="Escolher os canais (um ou vários) que recebem os avisos")
@app_commands.guild_only()
async def canal(inter: discord.Interaction):
    atuais = db.execute("SELECT channel_id FROM canais WHERE user_id=? AND guild_id=?",
                        (inter.user.id, inter.guild_id)).fetchall()
    txt = " ".join(f"<#{c}>" for (c,) in atuais) or "nenhum"
    await inter.response.send_message(
        f"📍 **Canais de aviso neste servidor:** {txt}\n\n"
        "Escolha abaixo **todos** os canais que devem receber os avisos. "
        "A escolha substitui a lista atual deste servidor.",
        view=CanaisView(), ephemeral=True)


# ---------------------------------------------------------------------------
# Seguir/remover vários animes de uma vez + lista numerada
# ---------------------------------------------------------------------------
def dividir(texto: str) -> list[str]:
    """Separa por vírgula, ponto e vírgula ou quebra de linha."""
    return [p.strip() for p in re.split(r"[,;\n]+", texto) if p.strip()]


async def buscar_anime(texto: str) -> Optional[dict]:
    q = """query($s:String){Media(search:$s,type:ANIME,sort:SEARCH_MATCH){id title{romaji english}}}"""
    try:
        return (await anilist(bot.session, q, {"s": texto}))["Media"]
    except Exception:
        return None


@bot.tree.command(name="seguir_varios", description="Seguir vários animes de uma vez (separe por vírgula)")
@app_commands.describe(nomes="Ex.: Jujutsu Kaisen, One Piece, Frieren")
async def seguir_varios(inter: discord.Interaction, nomes: str):
    itens = dividir(nomes)
    if not itens:
        return await inter.response.send_message("Escreva pelo menos um nome.", ephemeral=True)
    if len(itens) > 15:
        return await inter.response.send_message("Máximo de 15 nomes por vez.", ephemeral=True)

    await inter.response.defer(ephemeral=True)
    ok, falhas = [], []
    for item in itens:
        m = await buscar_anime(item)
        if m:
            db.execute("INSERT OR REPLACE INTO follows VALUES(?,?,?)", (inter.user.id, m["id"], nome(m["title"])))
            ok.append(f"• {item} → **{nome(m['title'])}**")
        else:
            falhas.append(item)
        await asyncio.sleep(0.7)  # respeita o limite de requisições do AniList
    db.commit()

    partes = []
    if ok:
        partes.append("✅ **Agora você segue:**\n" + "\n".join(ok))
    if falhas:
        partes.append("❌ **Não encontrei** (tente o nome em inglês ou romaji):\n" + "\n".join(f"• {f}" for f in falhas))
    partes.append("Confira se o anime encontrado é o certo. Se não for, remova com `/remover_varios`.")
    await inter.followup.send("\n\n".join(partes)[:1900])


@bot.tree.command(name="lista", description="Animes que você segue (numerados)")
async def lista(inter: discord.Interaction):
    rows = db.execute("SELECT title FROM follows WHERE user_id=? ORDER BY rowid", (inter.user.id,)).fetchall()
    if not rows:
        return await inter.response.send_message("Você ainda não segue nenhum anime.", ephemeral=True)
    txt = "\n".join(f"**{i}.** {t}" for i, (t,) in enumerate(rows, 1))
    await inter.response.send_message(txt[:1900], ephemeral=True)


@bot.tree.command(name="remover_varios", description="Remover vários animes por nome ou número da /lista")
@app_commands.describe(itens="Ex.: 1, 3, 5-7, nome do anime")
async def remover_varios(inter: discord.Interaction, itens: str):
    rows = db.execute("SELECT media_id, title FROM follows WHERE user_id=? ORDER BY rowid", (inter.user.id,)).fetchall()
    if not rows:
        return await inter.response.send_message("Você ainda não segue nenhum anime.", ephemeral=True)

    remover: dict[int, str] = {}
    problemas: list[str] = []
    for item in dividir(itens):
        faixa = re.fullmatch(r"(\d+)\s*-\s*(\d+)", item)
        if item.isdigit() or faixa:
            if faixa:
                a, b = sorted((int(faixa[1]), int(faixa[2])))
            else:
                a = b = int(item)
            if a < 1 or a > len(rows):
                problemas.append(f"nº {item}: não existe na lista")
                continue
            for n in range(a, min(b, len(rows)) + 1):
                remover[rows[n - 1][0]] = rows[n - 1][1]
        else:
            baixo = item.lower()
            achados = [r for r in rows if r[1].lower() == baixo] or [r for r in rows if baixo in r[1].lower()]
            if len(achados) == 1:
                remover[achados[0][0]] = achados[0][1]
            elif not achados:
                problemas.append(f"“{item}”: não encontrado na sua lista")
            else:
                problemas.append(f"“{item}”: bate com mais de um ({', '.join(r[1] for r in achados[:3])}…). Seja mais específico ou use o número")

    for mid in remover:
        db.execute("DELETE FROM follows WHERE user_id=? AND media_id=?", (inter.user.id, mid))
    db.commit()

    partes = []
    if remover:
        partes.append("🗑️ **Removidos:**\n" + "\n".join(f"• {t}" for t in remover.values()))
    if problemas:
        partes.append("⚠️ **Não consegui:**\n" + "\n".join(f"• {p}" for p in problemas))
    if remover:
        partes.append("Os números da lista mudam depois de remover. Use `/lista` para ver a nova.")
    await inter.response.send_message("\n\n".join(partes)[:1900], ephemeral=True)


# ---------------------------------------------------------------------------
# DM: só recebe aviso por mensagem privada quem for adicionado aqui
# ---------------------------------------------------------------------------
def extrair_ids(texto: str) -> list[int]:
    """Pega @menções (<@123>) e IDs soltos de um texto, sem repetir."""
    ids = [int(x) for x in re.findall(r"<@!?(\d{15,20})>", texto)]
    resto = re.sub(r"<@!?\d+>", " ", texto)
    ids += [int(x) for x in re.findall(r"\b\d{15,20}\b", resto)]
    return list(dict.fromkeys(ids))


def membros_do_dono(dono_id: int) -> list[int]:
    return [r[0] for r in db.execute("SELECT membro_id FROM membros WHERE dono_id=? ORDER BY rowid", (dono_id,))]


async def registrar_membros(dono: discord.abc.User, ids: list[int]) -> tuple[list[str], list[str]]:
    ok, falhas = [], []
    for uid in ids:
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
        except discord.HTTPException:
            falhas.append(f"<@{uid}>: usuário não encontrado")
            continue
        if user.bot:
            falhas.append(f"{user.mention}: bots não recebem DM")
            continue
        ja = db.execute("SELECT 1 FROM membros WHERE dono_id=? AND membro_id=?", (dono.id, uid)).fetchone()
        db.execute("INSERT OR IGNORE INTO membros VALUES(?,?)", (dono.id, uid))
        if ja:
            ok.append(f"{user.mention} (já estava na lista)")
            continue
        try:
            await user.send(
                f"👋 **{dono.display_name}** adicionou você para receber avisos de episódios de anime por DM. "
                "Para parar de receber, use o comando `/sair_dos_avisos` em um servidor onde o bot esteja.")
            ok.append(user.mention)
        except discord.HTTPException:
            ok.append(f"{user.mention} ⚠️ adicionado, mas não consegui enviar DM (DMs fechadas ou nenhum servidor em comum com o bot)")
    db.commit()
    return ok, falhas


def resumo_dm(ok: list[str], falhas: list[str]) -> str:
    partes = []
    if ok:
        partes.append("✅ **Receberão os avisos por DM:**\n" + "\n".join(f"• {x}" for x in ok))
    if falhas:
        partes.append("⚠️ **Não consegui:**\n" + "\n".join(f"• {x}" for x in falhas))
    return "\n\n".join(partes)[:1900] or "Nada a fazer."


class DmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Selecione uma ou mais pessoas",
                       min_values=1, max_values=25)
    async def escolher(self, inter: discord.Interaction, select: discord.ui.UserSelect):
        await inter.response.defer(ephemeral=True)
        ok, falhas = await registrar_membros(inter.user, [u.id for u in select.values])
        await inter.followup.send(resumo_dm(ok, falhas), ephemeral=True)


@bot.tree.command(name="dm", description="Adicionar membros que receberão os avisos por DM")
@app_commands.describe(membros="Mencione várias pessoas (@fulano @ciclano) ou cole os IDs. Vazio = escolher numa lista")
async def dm(inter: discord.Interaction, membros: Optional[str] = None):
    if not membros:
        return await inter.response.send_message(
            "Escolha abaixo quem vai receber os avisos por DM:", view=DmView(), ephemeral=True)
    ids = extrair_ids(membros)
    if not ids:
        return await inter.response.send_message(
            "Não encontrei nenhuma menção ou ID. Use @pessoa ou deixe o campo vazio para escolher numa lista.",
            ephemeral=True)
    if len(ids) > 25:
        return await inter.response.send_message("Máximo de 25 pessoas por vez.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    ok, falhas = await registrar_membros(inter.user, ids)
    await inter.followup.send(resumo_dm(ok, falhas))


@bot.tree.command(name="dm_lista", description="Mostra quem recebe os avisos por DM (numerado)")
async def dm_lista(inter: discord.Interaction):
    ids = membros_do_dono(inter.user.id)
    if not ids:
        return await inter.response.send_message("Ninguém recebe avisos por DM ainda. Use `/dm`.", ephemeral=True)
    txt = "\n".join(f"**{i}.** <@{u}>" for i, u in enumerate(ids, 1))
    await inter.response.send_message(txt[:1900], ephemeral=True)


@bot.tree.command(name="remover_dm", description="Remove pessoas dos avisos por DM (por @menção, ID ou número da /dm_lista)")
@app_commands.describe(itens="Ex.: @fulano @ciclano, 2, 4-5")
async def remover_dm(inter: discord.Interaction, itens: str):
    ids = membros_do_dono(inter.user.id)
    if not ids:
        return await inter.response.send_message("Ninguém recebe avisos por DM ainda.", ephemeral=True)

    remover: dict[int, None] = {}
    problemas: list[str] = []

    for uid in extrair_ids(itens):
        if uid in ids:
            remover[uid] = None
        else:
            problemas.append(f"<@{uid}>: não está na sua lista de DM")

    resto = re.sub(r"<@!?\d+>|\b\d{15,20}\b", " ", itens)
    resto = re.sub(r"\s*-\s*", "-", resto)
    for tok in [t for t in re.split(r"[,;\s]+", resto) if t]:
        faixa = re.fullmatch(r"(\d+)-(\d+)", tok)
        if tok.isdigit() or faixa:
            a, b = sorted((int(faixa[1]), int(faixa[2]))) if faixa else (int(tok), int(tok))
            if a < 1 or a > len(ids):
                problemas.append(f"nº {tok}: não existe na lista")
                continue
            for n in range(a, min(b, len(ids)) + 1):
                remover[ids[n - 1]] = None
        else:
            problemas.append(f"“{tok}”: não entendi (use @pessoa, ID ou número)")

    for uid in remover:
        db.execute("DELETE FROM membros WHERE dono_id=? AND membro_id=?", (inter.user.id, uid))
    db.commit()

    partes = []
    if remover:
        partes.append("🗑️ **Não receberão mais por DM:**\n" + "\n".join(f"• <@{u}>" for u in remover))
        partes.append("Os números mudam depois de remover. Use `/dm_lista` para ver a nova lista.")
    if problemas:
        partes.append("⚠️ **Não consegui:**\n" + "\n".join(f"• {p}" for p in problemas))
    await inter.response.send_message("\n\n".join(partes)[:1900], ephemeral=True)


@bot.tree.command(name="sair_dos_avisos", description="Para de receber por DM os avisos que outras pessoas configuraram para você")
async def sair_dos_avisos(inter: discord.Interaction):
    db.execute("DELETE FROM membros WHERE membro_id=?", (inter.user.id,))
    db.commit()
    await inter.response.send_message("✅ Você não receberá mais avisos por DM configurados por outras pessoas.", ephemeral=True)


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
# Biblioteca pessoal + leitor (arquivos que VOCÊ adiciona; só o dono do bot usa)
# O leitor é "efêmero": só você enxerga as páginas, mesmo lendo dentro de um canal.
# ---------------------------------------------------------------------------
try:
    import fitz  # PyMuPDF: opcional, só necessário para ler PDF (adicione `pymupdf` ao requirements.txt)
except ImportError:
    fitz = None

DADOS = os.getenv("DADOS_DIR", "dados")
PASTA_BIBLIOTECA = os.path.join(DADOS, "biblioteca")
PASTA_ENTRADA = os.path.join(DADOS, "entrada")  # coloque aqui arquivos grandes pelo painel/SFTP da hospedagem
LIMITE_MANGA_MB = 500
os.makedirs(PASTA_BIBLIOTECA, exist_ok=True)
os.makedirs(PASTA_ENTRADA, exist_ok=True)

# --- helpers puros (início) ---
EXT_IMG = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_cache_nomes: dict[str, list[str]] = {}


def chave_natural(txt: str) -> list:
    """Ordena 'pag2' antes de 'pag10'."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", txt.lower())]


def nomes_paginas(caminho: str) -> list[str]:
    if caminho in _cache_nomes:
        return _cache_nomes[caminho]
    with zipfile.ZipFile(caminho) as zf:
        nomes = [n for n in zf.namelist()
                 if n.lower().endswith(EXT_IMG)
                 and "__macosx" not in n.lower()
                 and not os.path.basename(n).startswith(".")]
    nomes.sort(key=chave_natural)
    _cache_nomes[caminho] = nomes
    return nomes


def descobrir_ext(caminho: str) -> Optional[str]:
    with open(caminho, "rb") as f:
        cabecalho = f.read(5)
    if cabecalho.startswith(b"%PDF"):
        return ".pdf"
    if zipfile.is_zipfile(caminho):
        return ".cbz"
    return None


def contar_paginas(caminho: str) -> int:
    if caminho.lower().endswith(".pdf"):
        if fitz is None:
            raise ValueError("Para ler PDF, adicione `pymupdf` ao requirements.txt e reinicie o bot.")
        with fitz.open(caminho) as doc:
            return doc.page_count
    if not zipfile.is_zipfile(caminho):
        raise ValueError("O arquivo não é um CBZ/ZIP válido.")
    n = len(nomes_paginas(caminho))
    if n == 0:
        raise ValueError("Não encontrei imagens (jpg, png, webp ou gif) dentro do arquivo.")
    return n


def ler_pagina(caminho: str, indice: int) -> tuple[bytes, str]:
    """Devolve (bytes da imagem, extensão sem ponto)."""
    if caminho.lower().endswith(".pdf"):
        with fitz.open(caminho) as doc:
            pix = doc.load_page(indice).get_pixmap(dpi=110)
            return pix.tobytes("jpeg"), "jpg"
    nomes = nomes_paginas(caminho)
    with zipfile.ZipFile(caminho) as zf:
        return zf.read(nomes[indice]), os.path.splitext(nomes[indice])[1].lower().lstrip(".")
# --- helpers puros (fim) ---


async def so_dono(inter: discord.Interaction) -> bool:
    if await eh_dono(inter.user):
        return True
    await inter.response.send_message("🚫 Só o dono do bot pode usar a biblioteca de mangás.", ephemeral=True)
    return False


async def baixar_para_biblioteca(arquivo: Optional[discord.Attachment], link: Optional[str],
                                 da_pasta: Optional[str]) -> str:
    """Traz o arquivo para a biblioteca e devolve o caminho final (com extensão correta)."""
    temp = os.path.join(PASTA_BIBLIOTECA, f"{uuid.uuid4().hex}.part")
    try:
        if arquivo:
            await arquivo.save(temp)
        elif link:
            if not link.startswith(("http://", "https://")):
                raise ValueError("O link precisa começar com http:// ou https://")
            limite = LIMITE_MANGA_MB * 1024 * 1024
            total = 0
            async with bot.session.get(link) as r:
                if r.status != 200:
                    raise ValueError(f"O link respondeu com erro {r.status}.")
                with open(temp, "wb") as f:
                    async for pedaco in r.content.iter_chunked(262144):
                        total += len(pedaco)
                        if total > limite:
                            raise ValueError(f"Arquivo maior que {LIMITE_MANGA_MB} MB.")
                        f.write(pedaco)
        else:
            origem = os.path.join(PASTA_ENTRADA, os.path.basename(da_pasta))
            if not os.path.isfile(origem):
                raise ValueError("Não encontrei esse arquivo na pasta de entrada.")
            shutil.move(origem, temp)

        ext = descobrir_ext(temp)
        if ext is None:
            raise ValueError("Formato não suportado. Use .cbz, .zip ou .pdf (CBR/RAR: converta para CBZ).")
        final = temp[: -len(".part")] + ext
        os.replace(temp, final)
        return final
    except Exception:
        if os.path.exists(temp):
            os.remove(temp)
        raise


async def entrada_ac(inter: discord.Interaction, atual: str):
    if not await eh_dono(inter.user):
        return []
    try:
        arqs = sorted(os.listdir(PASTA_ENTRADA))
    except OSError:
        return []
    return [app_commands.Choice(name=a[:100], value=a[:100]) for a in arqs
            if atual.lower() in a.lower() and a.lower().endswith((".cbz", ".zip", ".pdf"))][:25]


@bot.tree.command(name="biblioteca_add", description="Adiciona um mangá seu (CBZ, ZIP ou PDF) à biblioteca de leitura")
@app_commands.describe(
    titulo="Nome do mangá/volume",
    arquivo="Arquivo .cbz, .zip ou .pdf (vale o limite de upload do Discord)",
    link="Link direto para o arquivo (alternativa ao anexo)",
    da_pasta="Arquivo que você colocou na pasta 'entrada' do bot (para arquivos grandes)")
@app_commands.autocomplete(da_pasta=entrada_ac)
async def biblioteca_add(inter: discord.Interaction, titulo: str, arquivo: Optional[discord.Attachment] = None,
                         link: Optional[str] = None, da_pasta: Optional[str] = None):
    if not await so_dono(inter):
        return
    if sum(bool(x) for x in (arquivo, link, da_pasta)) != 1:
        return await inter.response.send_message(
            "Informe **uma** origem: o anexo `arquivo`, um `link` **ou** `da_pasta`.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    caminho = None
    try:
        caminho = await baixar_para_biblioteca(arquivo, link, da_pasta)
        total = await asyncio.to_thread(contar_paginas, caminho)
    except ValueError as e:
        if caminho and os.path.exists(caminho):
            os.remove(caminho)
        return await inter.followup.send(f"⚠️ {e}")
    except Exception as e:
        print("Erro ao adicionar mangá:", e)
        if caminho and os.path.exists(caminho):
            os.remove(caminho)
        return await inter.followup.send("⚠️ Não consegui adicionar esse arquivo. Confira se ele não está corrompido.")

    db.execute("INSERT INTO biblioteca(dono_id, titulo, caminho, paginas) VALUES(?,?,?,?)",
               (inter.user.id, titulo.strip()[:100], caminho, total))
    db.commit()
    await inter.followup.send(f"✅ **{titulo.strip()[:100]}** adicionado ({total} páginas). Abra com `/ler`.")


@bot.tree.command(name="biblioteca", description="Mostra os mangás da sua biblioteca (numerados)")
async def biblioteca(inter: discord.Interaction):
    if not await so_dono(inter):
        return
    rows = db.execute("SELECT id, titulo, paginas FROM biblioteca WHERE dono_id=? ORDER BY id", (inter.user.id,)).fetchall()
    if not rows:
        return await inter.response.send_message("Sua biblioteca está vazia. Use `/biblioteca_add`.", ephemeral=True)
    prog = dict(db.execute("SELECT manga_id, pagina FROM progresso WHERE user_id=?", (inter.user.id,)).fetchall())
    linhas = []
    for i, (mid, titulo, total) in enumerate(rows, 1):
        extra = f" • parou na pág. {prog[mid] + 1}" if mid in prog else ""
        linhas.append(f"**{i}.** {titulo} — {total} págs{extra}")
    await inter.response.send_message("\n".join(linhas)[:1900], ephemeral=True)


@bot.tree.command(name="biblioteca_remover", description="Remove mangás da biblioteca por nome, número ou faixa (ex.: 1, 3-5, naruto)")
@app_commands.describe(itens="Nomes e/ou números da /biblioteca, separados por vírgula")
async def biblioteca_remover(inter: discord.Interaction, itens: str):
    if not await so_dono(inter):
        return
    rows = db.execute("SELECT id, titulo, caminho FROM biblioteca WHERE dono_id=? ORDER BY id", (inter.user.id,)).fetchall()
    if not rows:
        return await inter.response.send_message("Sua biblioteca já está vazia.", ephemeral=True)

    remover: dict[int, tuple[str, str]] = {}
    problemas: list[str] = []
    for item in dividir(itens):
        faixa = re.fullmatch(r"(\d+)\s*-\s*(\d+)", item)
        if item.isdigit() or faixa:
            a, b = sorted((int(faixa[1]), int(faixa[2]))) if faixa else (int(item), int(item))
            if a < 1 or a > len(rows):
                problemas.append(f"nº {item}: não existe na lista")
                continue
            for n in range(a, min(b, len(rows)) + 1):
                remover[rows[n - 1][0]] = (rows[n - 1][1], rows[n - 1][2])
        else:
            baixo = item.lower()
            achados = [r for r in rows if r[1].lower() == baixo] or [r for r in rows if baixo in r[1].lower()]
            if len(achados) == 1:
                remover[achados[0][0]] = (achados[0][1], achados[0][2])
            elif not achados:
                problemas.append(f"“{item}”: não encontrado")
            else:
                problemas.append(f"“{item}”: bate com mais de um ({', '.join(r[1] for r in achados[:3])}…). Seja mais específico ou use o número")

    for mid, (_, caminho) in remover.items():
        db.execute("DELETE FROM biblioteca WHERE id=?", (mid,))
        db.execute("DELETE FROM progresso WHERE manga_id=?", (mid,))
        _cache_nomes.pop(caminho, None)
        try:
            os.remove(caminho)
        except OSError:
            pass
    db.commit()

    partes = []
    if remover:
        partes.append("🗑️ **Removidos (arquivo apagado do bot):**\n" + "\n".join(f"• {t}" for t, _ in remover.values()))
        partes.append("Os números mudam depois de remover. Use `/biblioteca` para ver a nova lista.")
    if problemas:
        partes.append("⚠️ **Não consegui:**\n" + "\n".join(f"• {p}" for p in problemas))
    await inter.response.send_message("\n\n".join(partes)[:1900], ephemeral=True)


def resolver_manga(dono_id: int, texto: str):
    """Devolve a linha (id, titulo, caminho, paginas) ou uma mensagem de erro (str)."""
    rows = db.execute("SELECT id, titulo, caminho, paginas FROM biblioteca WHERE dono_id=? ORDER BY id", (dono_id,)).fetchall()
    if not rows:
        return "Sua biblioteca está vazia. Use `/biblioteca_add`."
    t = texto.strip()
    if t.startswith("id:") and t[3:].isdigit():
        for r in rows:
            if r[0] == int(t[3:]):
                return r
        return "Mangá não encontrado."
    if t.isdigit():
        n = int(t)
        return rows[n - 1] if 1 <= n <= len(rows) else f"Não existe o nº {n} na biblioteca."
    baixo = t.lower()
    achados = [r for r in rows if r[1].lower() == baixo] or [r for r in rows if baixo in r[1].lower()]
    if len(achados) == 1:
        return achados[0]
    if not achados:
        return f"Não achei “{texto}” na biblioteca."
    return "Mais de um combina: " + ", ".join(r[1] for r in achados[:3]) + ". Seja mais específico ou use o número."


async def biblioteca_ac(inter: discord.Interaction, atual: str):
    if not await eh_dono(inter.user):
        return []
    rows = db.execute("SELECT id, titulo FROM biblioteca WHERE dono_id=? ORDER BY id", (inter.user.id,)).fetchall()
    return [app_commands.Choice(name=f"{i}. {t}"[:100], value=f"id:{mid}")
            for i, (mid, t) in enumerate(rows, 1) if atual.lower() in t.lower()][:25]


class IrParaModal(discord.ui.Modal, title="Ir para a página"):
    numero = discord.ui.TextInput(label="Número da página", placeholder="Ex.: 25", max_length=5)

    def __init__(self, leitor: "LeitorView"):
        super().__init__()
        self.leitor = leitor

    async def on_submit(self, inter: discord.Interaction):
        v = self.numero.value.strip()
        if not v.isdigit() or not 1 <= int(v) <= self.leitor.total:
            return await inter.response.send_message(
                f"Digite um número de 1 a {self.leitor.total}.", ephemeral=True)
        await self.leitor.ir_para(inter, int(v) - 1)


class LeitorView(discord.ui.View):
    def __init__(self, user_id: int, manga_id: int, titulo: str, caminho: str, total: int, pagina: int):
        super().__init__(timeout=1800)
        self.user_id, self.manga_id, self.titulo = user_id, manga_id, titulo
        self.caminho, self.total, self.pagina = caminho, total, max(0, min(total - 1, pagina))
        self._ajustar_botoes()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message("Este leitor pertence a outra pessoa.", ephemeral=True)
            return False
        return True

    def _ajustar_botoes(self):
        self.primeira.disabled = self.anterior.disabled = self.pagina <= 0
        self.proxima.disabled = self.ultima.disabled = self.pagina >= self.total - 1

    async def montar(self):
        dados, ext = await asyncio.to_thread(ler_pagina, self.caminho, self.pagina)
        nome_arq = f"p{self.pagina + 1}.{ext}"
        emb = discord.Embed(title=self.titulo,
                            description=f"Página **{self.pagina + 1}** de **{self.total}**", color=0x3F51B5)
        emb.set_image(url=f"attachment://{nome_arq}")
        arq = discord.File(io.BytesIO(dados), filename=nome_arq)
        db.execute("INSERT OR REPLACE INTO progresso VALUES(?,?,?)", (self.user_id, self.manga_id, self.pagina))
        db.commit()
        self._ajustar_botoes()
        return emb, arq

    async def ir_para(self, inter: discord.Interaction, nova: int):
        self.pagina = max(0, min(self.total - 1, nova))
        await inter.response.defer()
        try:
            emb, arq = await self.montar()
        except FileNotFoundError:
            return await inter.followup.send("⚠️ O arquivo sumiu do bot. Remova e adicione o mangá de novo.", ephemeral=True)
        except Exception as e:
            print("Erro ao abrir página:", e)
            return await inter.followup.send("⚠️ Não consegui abrir esta página.", ephemeral=True)
        try:
            await inter.edit_original_response(embed=emb, attachments=[arq], view=self)
        except discord.HTTPException:
            await inter.followup.send("⚠️ O Discord recusou esta página (talvez grande demais). Tente a próxima.", ephemeral=True)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def primeira(self, inter: discord.Interaction, button: discord.ui.Button):
        await self.ir_para(inter, 0)

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def anterior(self, inter: discord.Interaction, button: discord.ui.Button):
        await self.ir_para(inter, self.pagina - 1)

    @discord.ui.button(label="Próxima", emoji="▶️", style=discord.ButtonStyle.primary, row=0)
    async def proxima(self, inter: discord.Interaction, button: discord.ui.Button):
        await self.ir_para(inter, self.pagina + 1)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def ultima(self, inter: discord.Interaction, button: discord.ui.Button):
        await self.ir_para(inter, self.total - 1)

    @discord.ui.button(label="Ir para página", emoji="🔢", style=discord.ButtonStyle.secondary, row=1)
    async def ir(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(IrParaModal(self))

    @discord.ui.button(label="Fechar", emoji="✖️", style=discord.ButtonStyle.danger, row=1)
    async def fechar(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.edit_message(
            content="📕 Leitura fechada. Use `/ler` para continuar de onde parou.",
            embed=None, attachments=[], view=None)
        self.stop()


@bot.tree.command(name="ler", description="Abre um mangá da sua biblioteca para ler aqui (só você vê)")
@app_commands.describe(manga="Título ou número da /biblioteca",
                       pagina="Página inicial (opcional; sem isso, continua de onde você parou)")
@app_commands.autocomplete(manga=biblioteca_ac)
async def ler(inter: discord.Interaction, manga: str, pagina: Optional[int] = None):
    if not await so_dono(inter):
        return
    achado = resolver_manga(inter.user.id, manga)
    if isinstance(achado, str):
        return await inter.response.send_message(achado, ephemeral=True)
    mid, titulo, caminho, total = achado
    if not os.path.exists(caminho):
        return await inter.response.send_message(
            "⚠️ O arquivo desse mangá sumiu do bot. Remova com `/biblioteca_remover` e adicione de novo.", ephemeral=True)

    if pagina:
        inicio = pagina - 1
    else:
        salvo = db.execute("SELECT pagina FROM progresso WHERE user_id=? AND manga_id=?", (inter.user.id, mid)).fetchone()
        inicio = salvo[0] if salvo else 0

    await inter.response.defer(ephemeral=True)
    leitor = LeitorView(inter.user.id, mid, titulo, caminho, total, inicio)
    try:
        emb, arq = await leitor.montar()
    except Exception as e:
        print("Erro ao abrir mangá:", e)
        return await inter.followup.send("⚠️ Não consegui abrir esse arquivo. Ele pode estar corrompido.")
    await inter.followup.send(embed=emb, file=arq, view=leitor, ephemeral=True)


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


GENEROS_PT = {
    "Action": "Ação", "Adventure": "Aventura", "Comedy": "Comédia", "Drama": "Drama",
    "Ecchi": "Ecchi", "Fantasy": "Fantasia", "Horror": "Terror", "Mahou Shoujo": "Garota mágica",
    "Mecha": "Mecha", "Music": "Música", "Mystery": "Mistério", "Psychological": "Psicológico",
    "Romance": "Romance", "Sci-Fi": "Ficção científica", "Slice of Life": "Cotidiano",
    "Sports": "Esportes", "Supernatural": "Sobrenatural", "Thriller": "Suspense",
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
        emb.add_field(name="Gêneros", value=", ".join(GENEROS_PT.get(g, g) for g in m["genres"][:6]), inline=False)
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


def montar_aviso(ep: dict, modo: str):
    """modo: 'lancamento', 'lembrete' ou 'teste'. Devolve (embed, links)."""
    m = ep["media"]
    links = links_de_streaming(m)
    nm = nome(m["title"])
    if modo == "lembrete":
        titulo = f"⏰ {nm} — Ep. {ep['episode']} sai em cerca de 1 hora"
        desc = f"Estreia <t:{ep['airingAt']}:R> (às <t:{ep['airingAt']}:t>)."
        cor = 0xFFC107
    elif modo == "teste":
        titulo = f"🧪 TESTE — {nm} — Episódio {ep['episode']}"
        desc = f"Este é só um teste. O episódio saiu <t:{ep['airingAt']}:R>."
        cor = 0x4CAF50
    else:
        titulo = f"📺 {nm} — Episódio {ep['episode']}"
        desc = "Acabou de ser lançado!"
        cor = 0xE91E63

    emb = discord.Embed(title=titulo, url=m["siteUrl"], description=desc, color=cor)
    emb.set_thumbnail(url=m["coverImage"]["large"])
    return emb, links


def kw_aviso(emb: discord.Embed, links: list[dict], com_botoes: bool) -> dict:
    """Monta os argumentos do send(). Cria uma View nova a cada chamada."""
    kwargs = {"embed": emb}
    if links and com_botoes:
        view = discord.ui.View()
        for l in links:
            view.add_item(discord.ui.Button(label=f"Assistir na {l['site']}"[:80], url=l["url"]))
        kwargs["view"] = view
    return kwargs


async def enviar(ep: dict, lembrete: bool):
    m = ep["media"]
    emb, links = montar_aviso(ep, "lembrete" if lembrete else "lancamento")
    botoes = not lembrete

    canais: dict[int, set[int]] = {}  # canal -> pessoas a mencionar
    dms: set[int] = set()             # quem recebe por DM (só quem foi adicionado em /dm)
    for (dono,) in db.execute("SELECT user_id FROM follows WHERE media_id=?", (m["id"],)).fetchall():
        for (cid,) in db.execute("SELECT channel_id FROM canais WHERE user_id=?", (dono,)).fetchall():
            canais.setdefault(cid, set()).add(dono)
        for (uid,) in db.execute("SELECT membro_id FROM membros WHERE dono_id=?", (dono,)).fetchall():
            dms.add(uid)

    for cid, donos in canais.items():
        canal_obj = bot.get_channel(cid)
        if not canal_obj:
            continue
        try:
            await canal_obj.send(" ".join(f"<@{u}>" for u in donos), **kw_aviso(emb, links, botoes))
        except discord.HTTPException as e:
            print(f"Falha ao avisar no canal {cid}: {e}")

    for uid in dms:
        try:
            user = await bot.fetch_user(uid)
            await user.send(**kw_aviso(emb, links, botoes))
        except discord.HTTPException as e:
            print(f"Falha ao enviar DM para {uid}: {e}")


# ---------------------------------------------------------------------------
# /testar: botão que envia, neste canal, o episódio mais recente já lançado
# ---------------------------------------------------------------------------
async def ultimo_episodio(media_id: int) -> Optional[dict]:
    q = """query($id:Int,$now:Int){Page(perPage:1){
        airingSchedules(mediaId:$id,airingAt_lesser:$now,sort:TIME_DESC){
            episode airingAt
            media{id title{romaji english} siteUrl coverImage{large}
                  externalLinks{site url type}}}}}"""
    data = await anilist(bot.session, q, {"id": media_id, "now": int(time.time())})
    lista_ep = data["Page"]["airingSchedules"]
    return lista_ep[0] if lista_ep else None


class TesteView(discord.ui.View):
    def __init__(self, media_id: Optional[int]):
        super().__init__(timeout=300)
        self.media_id = media_id

    @discord.ui.button(label="Enviar aviso de teste", emoji="🧪", style=discord.ButtonStyle.primary)
    async def enviar_teste(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.defer(ephemeral=True)
        if self.media_id:
            ids = [self.media_id]
        else:
            ids = [r[0] for r in db.execute(
                "SELECT media_id FROM follows WHERE user_id=? ORDER BY rowid", (inter.user.id,))]

        ep = None
        for mid in ids:
            try:
                ep = await ultimo_episodio(mid)
            except Exception as e:
                print("Erro no teste:", e)
            if ep:
                break
        if not ep:
            return await inter.followup.send("Não achei nenhum episódio já lançado nos animes escolhidos.")

        emb, links = montar_aviso(ep, "teste")
        try:
            await inter.channel.send(**kw_aviso(emb, links, True))
        except discord.Forbidden:
            return await inter.followup.send(
                "⚠️ Não tenho permissão para enviar mensagens neste canal. "
                "Dê ao bot **Ver canais**, **Enviar mensagens** e **Incorporar links** aqui.")
        await inter.followup.send("✅ Teste enviado neste canal.")


@bot.tree.command(name="testar", description="Envia neste canal um aviso de teste com o último episódio lançado")
@app_commands.describe(anime="Anime para testar (opcional; sem escolher, usa o primeiro da sua lista com episódio lançado)")
@app_commands.autocomplete(anime=seguindo_ac)
async def testar(inter: discord.Interaction, anime: Optional[str] = None):
    media_id = int(anime) if anime and anime.isdigit() else None
    await inter.response.send_message(
        "Clique no botão para enviar um aviso de teste **neste canal**, com os botões de assistir.",
        view=TesteView(media_id), ephemeral=True)


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
