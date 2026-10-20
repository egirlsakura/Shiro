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
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import unicodedata

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.getenv("DISCORD_TOKEN", "COLE_SEU_TOKEN_AQUI")
ANILIST = "https://graphql.anilist.co"
DB = "animes.db"
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_API = "https://api.themoviedb.org/3"
TVMAZE_API = "https://api.tvmaze.com"
FUSO_BR = ZoneInfo("America/Sao_Paulo")
TIPOS_ALERTA = ("Dorama", "K-drama", "Série", "Filme", "Anime", "Filme anime", "Reality / programa", "Outro")
TIPOS_TV = {"Dorama", "K-drama", "Série", "Anime", "Reality / programa", "Outro"}
TIPOS_FILME = {"Filme", "Filme anime"}
# Limite de recuperação se a hospedagem ficar offline por alguns dias.
JANELA_ATRASO_DIAS = 5

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
    CREATE TABLE IF NOT EXISTS sorteio_estado(
        guild_id INTEGER PRIMARY KEY, aberto INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sorteio_participantes(
        guild_id INTEGER, user_id INTEGER, enviado INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS sorteio_nomes(
        guild_id INTEGER, user_id INTEGER, slot INTEGER, nome TEXT,
        PRIMARY KEY(guild_id, user_id, slot));
    CREATE TABLE IF NOT EXISTS conteudos_seguidos(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        titulo TEXT NOT NULL, tipo TEXT NOT NULL, plataforma TEXT NOT NULL,
        criado_em INTEGER NOT NULL,
        UNIQUE(user_id, titulo, tipo, plataforma));
    """
)
db.commit()

# Migração não destrutiva: preserva biblioteca, sorteios, animes e listas antigas.
db.executescript("""
    CREATE TABLE IF NOT EXISTS canais_conteudo(
        user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
        tipo TEXT NOT NULL, channel_id INTEGER NOT NULL,
        PRIMARY KEY(user_id, guild_id, tipo, channel_id));
    CREATE TABLE IF NOT EXISTS dm_categorias(
        dono_id INTEGER NOT NULL, membro_id INTEGER NOT NULL, tipo TEXT NOT NULL,
        PRIMARY KEY(dono_id, membro_id, tipo));
    CREATE TABLE IF NOT EXISTS dm_bloqueios(user_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS alertas_entregues(
        destino TEXT NOT NULL, destino_id INTEGER NOT NULL,
        origem TEXT NOT NULL, mid INTEGER NOT NULL,
        episodio TEXT NOT NULL, tipo TEXT NOT NULL,
        PRIMARY KEY(destino, destino_id, origem, mid, episodio, tipo));
""")
colunas = {r[1] for r in db.execute("PRAGMA table_info(conteudos_seguidos)")}
for coluna, definicao in (
    ("origem", "TEXT"), ("mid", "INTEGER"), ("tvmaze_id", "INTEGER"),
    ("poster_url", "TEXT")
):
    if coluna not in colunas:
        db.execute(f"ALTER TABLE conteudos_seguidos ADD COLUMN {coluna} {definicao}")
# Quem já estava cadastrado no antigo /dm continua recebendo animes.
db.execute("""INSERT OR IGNORE INTO dm_categorias(dono_id,membro_id,tipo)
              SELECT dono_id,membro_id,'Anime' FROM membros
              WHERE membro_id NOT IN (SELECT user_id FROM dm_bloqueios)""")
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
        checar_conteudos.start()


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
# Sorteio comunitário: cada participante cadastra cinco nomes/tickets.
# O sorteio escolhe um ticket e menciona o membro que o cadastrou.
# ---------------------------------------------------------------------------
async def sorteio_aberto(guild_id: int) -> bool:
    row = db.execute("SELECT aberto FROM sorteio_estado WHERE guild_id=?", (guild_id,)).fetchone()
    return bool(row and row[0])


@bot.tree.command(name="sorteio_abrir", description="Abre um novo sorteio e limpa as inscrições anteriores")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def sorteio_abrir(inter: discord.Interaction):
    db.execute("DELETE FROM sorteio_nomes WHERE guild_id=?", (inter.guild_id,))
    db.execute("DELETE FROM sorteio_participantes WHERE guild_id=?", (inter.guild_id,))
    db.execute("INSERT OR REPLACE INTO sorteio_estado(guild_id, aberto) VALUES(?,1)", (inter.guild_id,))
    db.commit()
    await inter.response.send_message(
        "🎟️ **Novo sorteio aberto!** Use `/sorteio_entrar` para participar e depois "
        "`/sorteio_nomes` para enviar seus 5 nomes. Um administrador inicia com `/sorteio_iniciar`.")


@bot.tree.command(name="sorteio_entrar", description="Inscreve você no sorteio atual")
@app_commands.guild_only()
async def sorteio_entrar(inter: discord.Interaction):
    if not await sorteio_aberto(inter.guild_id):
        return await inter.response.send_message("Não há sorteio aberto. Peça a um administrador para usar `/sorteio_abrir`.", ephemeral=True)
    db.execute("INSERT OR IGNORE INTO sorteio_participantes(guild_id,user_id,enviado) VALUES(?,?,0)",
               (inter.guild_id, inter.user.id))
    db.commit()
    await inter.response.send_message(
        "✅ Você está inscrito! Agora use `/sorteio_nomes` e preencha os cinco campos com seus nomes.", ephemeral=True)


@bot.tree.command(name="sorteio_nomes", description="Envia os cinco nomes/tickets da sua participação")
@app_commands.guild_only()
@app_commands.describe(nome1="Primeiro nome/ticket", nome2="Segundo nome/ticket", nome3="Terceiro nome/ticket",
                       nome4="Quarto nome/ticket", nome5="Quinto nome/ticket")
async def sorteio_nomes(inter: discord.Interaction, nome1: str, nome2: str, nome3: str, nome4: str, nome5: str):
    if not await sorteio_aberto(inter.guild_id):
        return await inter.response.send_message("Não há sorteio aberto. Peça a um administrador para usar `/sorteio_abrir`.", ephemeral=True)
    row = db.execute("SELECT enviado FROM sorteio_participantes WHERE guild_id=? AND user_id=?",
                     (inter.guild_id, inter.user.id)).fetchone()
    if not row:
        return await inter.response.send_message("Primeiro entre no sorteio com `/sorteio_entrar`.", ephemeral=True)
    if row[0]:
        return await inter.response.send_message("Você já enviou seus cinco nomes. Não é possível alterar durante este sorteio.", ephemeral=True)
    nomes = [x.strip() for x in (nome1, nome2, nome3, nome4, nome5)]
    if any(not x for x in nomes):
        return await inter.response.send_message("Os cinco nomes precisam estar preenchidos.", ephemeral=True)
    if any(len(x) > 100 for x in nomes):
        return await inter.response.send_message("Cada nome pode ter no máximo 100 caracteres.", ephemeral=True)
    for slot, nome_txt in enumerate(nomes, 1):
        db.execute("INSERT INTO sorteio_nomes(guild_id,user_id,slot,nome) VALUES(?,?,?,?)",
                   (inter.guild_id, inter.user.id, slot, nome_txt))
    db.execute("UPDATE sorteio_participantes SET enviado=1 WHERE guild_id=? AND user_id=?",
               (inter.guild_id, inter.user.id))
    db.commit()
    await inter.response.send_message("✅ Seus 5 nomes foram registrados! Use `/sorteio_status` para conferir o andamento.", ephemeral=True)


@bot.tree.command(name="sorteio_status", description="Mostra quem já enviou os cinco nomes para o sorteio")
@app_commands.guild_only()
async def sorteio_status(inter: discord.Interaction):
    if not await sorteio_aberto(inter.guild_id):
        return await inter.response.send_message("Não há sorteio aberto no momento.", ephemeral=True)
    rows = db.execute("SELECT user_id,enviado FROM sorteio_participantes WHERE guild_id=? ORDER BY user_id",
                      (inter.guild_id,)).fetchall()
    if not rows:
        return await inter.response.send_message("Ainda não há participantes. Use `/sorteio_entrar` para se inscrever.", ephemeral=True)
    enviados = sum(1 for _, enviado in rows if enviado)
    linhas = [f"{'✅' if enviado else '⏳'} <@{uid}> {'enviou os 5 nomes' if enviado else 'ainda não enviou os nomes'}"
              for uid, enviado in rows]
    await inter.response.send_message(
        f"**Participantes completos:** {enviados}/{len(rows)}\n" + "\n".join(linhas),
        allowed_mentions=discord.AllowedMentions.none(), ephemeral=True)


@bot.tree.command(name="sorteio_iniciar", description="Inicia o sorteio quando todos os inscritos completarem os cinco nomes")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def sorteio_iniciar(inter: discord.Interaction):
    if not await sorteio_aberto(inter.guild_id):
        return await inter.response.send_message("Não há sorteio aberto. Use `/sorteio_abrir` primeiro.", ephemeral=True)
    participantes = db.execute("SELECT user_id,enviado FROM sorteio_participantes WHERE guild_id=?",
                               (inter.guild_id,)).fetchall()
    if not participantes:
        return await inter.response.send_message("Ainda não há participantes inscritos.", ephemeral=True)
    pendentes = [uid for uid, enviado in participantes if not enviado]
    if pendentes:
        return await inter.response.send_message(
            "⏳ Ainda não posso iniciar. Estes participantes precisam enviar os cinco nomes: " +
            ", ".join(f"<@{uid}>" for uid in pendentes), ephemeral=True)
    entradas = db.execute("SELECT user_id,nome FROM sorteio_nomes WHERE guild_id=? ORDER BY user_id,slot",
                          (inter.guild_id,)).fetchall()
    if len(entradas) != len(participantes) * 5:
        return await inter.response.send_message("Os dados do sorteio estão incompletos. Peça aos participantes que confiram o status.", ephemeral=True)
    import random
    vencedor_id, nome_sorteado = random.SystemRandom().choice(entradas)
    db.execute("UPDATE sorteio_estado SET aberto=0 WHERE guild_id=?", (inter.guild_id,))
    db.commit()
    await inter.response.send_message(
        f"🎉 **SORTEIO CONCLUÍDO!**\nNome sorteado: **{discord.utils.escape_markdown(nome_sorteado)}**\n"
        f"🏆 Vencedor: <@{vencedor_id}>!", allowed_mentions=discord.AllowedMentions(users=True))


# ---------------------------------------------------------------------------
# Catálogo TMDB + TVmaze, acompanhamento por usuário e canais por categoria
# ---------------------------------------------------------------------------
TIPOS_CONTEUDO = [app_commands.Choice(name=x, value=x) for x in TIPOS_ALERTA]


async def api_json(url: str, params: Optional[dict] = None):
    """Não repassa credenciais aos logs; propagação de falhas para reprocessar."""
    async with bot.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status == 404:
            return None
        if r.status == 429:
            raise RuntimeError("Limite de requisições da API atingido (429)")
        r.raise_for_status()
        return await r.json()


async def tmdb_json(endpoint: str, extra: Optional[dict] = None):
    if not TMDB_API_KEY:
        raise RuntimeError("A chave TMDB_API_KEY não foi configurada na FadeHost.")
    params = {"api_key": TMDB_API_KEY, "language": "pt-BR"}
    if extra:
        params.update(extra)
    return await api_json(f"{TMDB_API}{endpoint}", params)


def normalizar(txt: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", txt.lower()).encode("ascii", "ignore").decode())


def kind(tipo: str) -> str:
    return "movie" if tipo in TIPOS_FILME else "tv"


async def encontrar_tvmaze(nome_txt: str, tmdb_id: Optional[int] = None, ano: Optional[str] = None):
    """Evita associar a outra obra de mesmo nome (prioriza identificador IMDb)."""
    if tmdb_id:
        try:
            ext = await tmdb_json(f"/tv/{tmdb_id}/external_ids")
            imdb = (ext or {}).get("imdb_id")
            if imdb:
                match = await api_json(f"{TVMAZE_API}/lookup/shows", {"imdb": imdb})
                if match:
                    return int(match["id"])
        except Exception as e:
            print("TVmaze: identificação externa indisponível:", type(e).__name__)
    try:
        resultados = await api_json(f"{TVMAZE_API}/search/shows", {"q": nome_txt}) or []
        for item in resultados[:10]:
            show = item.get("show") or {}
            if normalizar(show.get("name") or "") != normalizar(nome_txt):
                continue
            estreno = show.get("premiered") or ""
            if ano and estreno[:4] and abs(int(estreno[:4]) - int(ano[:4])) > 1:
                continue
            return int(show["id"])
    except Exception as e:
        print("Falha ao buscar TVmaze:", type(e).__name__)
    return None


class ResultadosConteudo(discord.ui.View):
    """Confirma o título escolhido para não acompanhar um homônimo por engano."""
    def __init__(self, usuario: int, titulo: str, tipo: str, plataforma: str, resultados: list):
        super().__init__(timeout=180)
        self.usuario, self.titulo, self.tipo, self.plataforma = usuario, titulo, tipo, plataforma
        self.resultados = {f"{k}:{i}": r for i, (k, r) in enumerate(resultados)}
        opcoes = []
        for value, (provider, r) in zip(self.resultados, resultados):
            tit = (r.get("name") or r.get("title") or "Sem título")
            ano = (r.get("first_air_date") or r.get("release_date") or r.get("premiered") or "????")[:4]
            opcoes.append(discord.SelectOption(label=f"{tit[:80]} ({ano})", value=value,
                                                  description="TMDB" if provider == "tmdb" else "TVmaze"))
        select = discord.ui.Select(placeholder="Escolha a obra correta", options=opcoes, min_values=1, max_values=1)
        select.callback = self.escolher
        self.add_item(select)

    async def interaction_check(self, inter: discord.Interaction):
        if inter.user.id != self.usuario:
            await inter.response.send_message("Somente quem iniciou pode escolher.", ephemeral=True)
            return False
        return True

    async def escolher(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        valor = inter.data["values"][0]
        r = self.resultados[valor]
        provider = valor.split(":", 1)[0]
        titulo = r.get("name") or r.get("title") or self.titulo
        mid = int(r["id"])
        tvmaze_id = mid if provider == "tvmaze" else None
        img = r.get("image") if provider == "tvmaze" else None
        poster_url = ((img or {}).get("medium") if isinstance(img, dict) else None) if provider == "tvmaze" else (
            "https://image.tmdb.org/t/p/w500" + r["poster_path"] if r.get("poster_path") else None)
        if provider == "tmdb" and self.tipo in TIPOS_TV:
            tvmaze_id = await encontrar_tvmaze(titulo, mid, r.get("first_air_date"))
        antigo = db.execute("""SELECT id FROM conteudos_seguidos
                               WHERE user_id=? AND lower(titulo)=lower(?) AND tipo=?""",
                            (inter.user.id, titulo, self.tipo)).fetchone()
        try:
            if antigo:
                db.execute("""UPDATE conteudos_seguidos
                              SET plataforma=?, origem=?, mid=?, tvmaze_id=?, poster_url=? WHERE id=?""",
                           (self.plataforma, provider, mid, tvmaze_id, poster_url, antigo[0]))
            else:
                db.execute("""INSERT INTO conteudos_seguidos
                   (user_id,titulo,tipo,plataforma,criado_em,origem,mid,tvmaze_id,poster_url)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                   (inter.user.id, titulo, self.tipo, self.plataforma, int(time.time()), provider, mid, tvmaze_id, poster_url))
            db.commit()
        except sqlite3.IntegrityError:
            return await inter.followup.send("Esse título já está na sua lista. Use `/meus_conteudos`.", ephemeral=True)
        self.stop()
        await inter.edit_original_response(content="✅ **Acompanhamento ativado!**\n"
                                           f"**{discord.utils.escape_markdown(titulo)}** · {self.tipo} · {self.plataforma}\n"
                                           "Escolha os canais em `/canal_conteudo` ou os destinatários em `/dm`.\n"
                                           "Filmes recebem aviso de **estreia**, não de episódios.", view=None)


@bot.tree.command(name="seguir_conteudo", description="Seguir doramas, K-dramas, séries, filmes e animes (TMDB/TVmaze)")
@app_commands.describe(titulo="Nome da obra", tipo="Categoria", plataforma="Streaming onde você pretende assistir")
@app_commands.choices(tipo=TIPOS_CONTEUDO)
async def seguir_conteudo(inter: discord.Interaction, titulo: str, tipo: app_commands.Choice[str], plataforma: str):
    titulo, plataforma = titulo.strip(), plataforma.strip()
    if not titulo or not plataforma or len(titulo) > 150 or len(plataforma) > 100:
        return await inter.response.send_message("Informe título e plataforma válidos (até 150 e 100 caracteres).", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    resultados = []
    if TMDB_API_KEY:
        try:
            data = await tmdb_json(f"/search/{kind(tipo.value)}", {"query": titulo, "include_adult": "false"})
            resultados = [("tmdb", x) for x in (data or {}).get("results", [])[:10]]
        except Exception as e:
            print("Pesquisa TMDB falhou:", type(e).__name__)
    if tipo.value in TIPOS_TV and len(resultados) < 5:
        try:
            shows = await api_json(f"{TVMAZE_API}/search/shows", {"q": titulo}) or []
            resultados.extend(("tvmaze", x["show"]) for x in shows[:max(0, 10-len(resultados))] if x.get("show"))
        except Exception as e:
            print("Pesquisa TVmaze falhou:", type(e).__name__)
    if not resultados:
        detalhe = " Configure TMDB_API_KEY na FadeHost." if not TMDB_API_KEY else ""
        return await inter.followup.send("Não encontrei esse título nas APIs." + detalhe, ephemeral=True)
    await inter.followup.send("🔎 **Selecione o título correto** para acompanhar seus lançamentos:",
                              view=ResultadosConteudo(inter.user.id, titulo, tipo.value, plataforma, resultados[:25]),
                              ephemeral=True)


@bot.tree.command(name="meus_conteudos", description="Lista numerada dos conteúdos que você acompanha")
async def meus_conteudos(inter: discord.Interaction):
    rows = db.execute("""SELECT titulo,tipo,plataforma,origem,mid
                         FROM conteudos_seguidos WHERE user_id=? ORDER BY id""",
                      (inter.user.id,)).fetchall()
    if not rows:
        return await inter.response.send_message("Você ainda não acompanha títulos. Use `/seguir_conteudo`.", ephemeral=True)
    linhas = [f"**{n}.** {discord.utils.escape_markdown(t)} · {tipo} · {plat}"
              + (" ⚠️ recadastre para ativar os alertas" if not orig or not mid else "")
              for n, (t, tipo, plat, orig, mid) in enumerate(rows, 1)]
    await inter.response.send_message("\n".join(linhas)[:1900], ephemeral=True)


@bot.tree.command(name="deixar_conteudo", description="Remove um título da sua lista pelo número")
@app_commands.describe(numero="Número exibido em /meus_conteudos")
async def deixar_conteudo(inter: discord.Interaction, numero: app_commands.Range[int, 1, 100000]):
    rows = db.execute("SELECT id,titulo FROM conteudos_seguidos WHERE user_id=? ORDER BY id",
                      (inter.user.id,)).fetchall()
    if numero > len(rows):
        return await inter.response.send_message("Esse número não existe na lista.", ephemeral=True)
    item_id, titulo = rows[numero - 1]
    db.execute("DELETE FROM conteudos_seguidos WHERE id=? AND user_id=?", (item_id, inter.user.id))
    db.commit()
    await inter.response.send_message(f"🗑️ **{discord.utils.escape_markdown(titulo)}** removido da sua lista.", ephemeral=True)


class EscolherCanaisConteudo(discord.ui.View):
    def __init__(self, usuario: int, tipo: str):
        super().__init__(timeout=240)
        self.usuario, self.tipo = usuario, tipo
        selecionador = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                    placeholder=f"Canais para {tipo}", min_values=1, max_values=25)
        selecionador.callback = self.salvar
        self.add_item(selecionador)
        self.add_item(LimparCanaisConteudo(usuario, tipo))

    async def interaction_check(self, inter: discord.Interaction):
        if inter.user.id != self.usuario:
            await inter.response.send_message("Somente quem iniciou pode alterar estes canais.", ephemeral=True)
            return False
        return True

    async def salvar(self, inter: discord.Interaction):
        canais = inter.data.get("values", [])
        if not inter.guild:
            return await inter.response.send_message("Use este comando dentro de um servidor.", ephemeral=True)
        db.execute("DELETE FROM canais_conteudo WHERE user_id=? AND guild_id=? AND tipo=?",
                   (inter.user.id, inter.guild_id, self.tipo))
        falhas = []
        salvos = []
        for cid in canais:
            cid = int(cid)
            c = inter.guild.get_channel(cid)
            if not c:
                try:
                    c = await inter.guild.fetch_channel(cid)
                except discord.HTTPException:
                    c = None
            if not c or not all((c.permissions_for(inter.guild.me).view_channel,
                                 c.permissions_for(inter.guild.me).send_messages,
                                 c.permissions_for(inter.guild.me).embed_links)):
                falhas.append(f"<#{cid}>")
                continue
            salvos.append(cid)
            db.execute("INSERT OR IGNORE INTO canais_conteudo VALUES(?,?,?,?)",
                       (inter.user.id, inter.guild_id, self.tipo, cid))
        db.commit()
        txt = " ".join(f"<#{int(cid)}>" for cid in salvos) or "nenhum"
        extra = "\n⚠️ Sem permissões nesses canais: " + ", ".join(falhas) if falhas else ""
        await inter.response.edit_message(content=f"✅ Canais de **{self.tipo}** configurados: {txt}{extra}", view=None)


class LimparCanaisConteudo(discord.ui.Button):
    def __init__(self, usuario: int, tipo: str):
        super().__init__(label="Remover canais desta categoria", emoji="🗑️", style=discord.ButtonStyle.danger)
        self.usuario, self.tipo = usuario, tipo

    async def callback(self, inter: discord.Interaction):
        db.execute("DELETE FROM canais_conteudo WHERE user_id=? AND guild_id=? AND tipo=?",
                   (self.usuario, inter.guild_id, self.tipo))
        db.commit()
        await inter.response.edit_message(content=f"🗑️ Canais de **{self.tipo}** removidos.", view=None)


class EscolherCategoriaCanal(discord.ui.View):
    def __init__(self, usuario: int):
        super().__init__(timeout=240)
        self.usuario = usuario
        select = discord.ui.Select(placeholder="Selecione a categoria dos avisos",
                options=[discord.SelectOption(label=t, value=t) for t in TIPOS_ALERTA])
        select.callback = self.proximo
        self.add_item(select)

    async def proximo(self, inter: discord.Interaction):
        if inter.user.id != self.usuario:
            return await inter.response.send_message("Seleção exclusiva de quem iniciou.", ephemeral=True)
        tipo = inter.data["values"][0]
        rows = db.execute("""SELECT channel_id FROM canais_conteudo
                             WHERE user_id=? AND guild_id=? AND tipo=?""",
                          (inter.user.id, inter.guild_id, tipo)).fetchall()
        txt = " ".join(f"<#{cid}>" for (cid,) in rows) or "nenhum"
        await inter.response.edit_message(content=f"📍 **{tipo}**: canais atuais: {txt}\n"
                                          "Selecione os canais para substituir essa lista:",
                                          view=EscolherCanaisConteudo(inter.user.id, tipo))


@bot.tree.command(name="canal_conteudo", description="Escolher categoria e canais para receber avisos de lançamentos")
@app_commands.guild_only()
async def canal_conteudo(inter: discord.Interaction):
    await inter.response.send_message("Selecione a categoria que deseja configurar:",
                                      view=EscolherCategoriaCanal(inter.user.id), ephemeral=True)


@bot.tree.command(name="canais_conteudo", description="Ver seus canais de aviso por categoria")
@app_commands.guild_only()
async def canais_conteudo(inter: discord.Interaction):
    rows = db.execute("""SELECT tipo,channel_id FROM canais_conteudo WHERE user_id=? AND guild_id=?
                         ORDER BY tipo,channel_id""", (inter.user.id, inter.guild_id)).fetchall()
    if not rows:
        return await inter.response.send_message("Nenhum canal escolhido. Use `/canal_conteudo`.", ephemeral=True)
    resp = {}
    for tipo, cid in rows:
        resp.setdefault(tipo, []).append(f"<#{cid}>")
    await inter.response.send_message("\n".join(f"**{t}:** {' '.join(c)}" for t,c in resp.items()), ephemeral=True)


# ---------------------------------------------------------------------------
# DM por categoria: qualquer membro pode usar; destinatário pode se descadastrar.
# ---------------------------------------------------------------------------
def extrair_ids(texto: str) -> list[int]:
    ids = [int(x) for x in re.findall(r"<@!?(\d{15,20})>", texto)]
    resto = re.sub(r"<@!?\d+>", " ", texto)
    ids += [int(x) for x in re.findall(r"\b\d{15,20}\b", resto)]
    return list(dict.fromkeys(ids))


def membros_do_dono(dono_id: int) -> list[int]:
    return [r[0] for r in db.execute("""SELECT DISTINCT membro_id FROM dm_categorias
              WHERE dono_id=? ORDER BY membro_id""", (dono_id,))]


async def registrar_membros(inter: discord.Interaction, ids: list[int], tipos: list[str]):
    ok, falhas = [], []
    for uid in ids:
        if db.execute("SELECT 1 FROM dm_bloqueios WHERE user_id=?", (uid,)).fetchone():
            falhas.append(f"<@{uid}> não permite notificações por DM")
            continue
        try:
            membro = await inter.guild.fetch_member(uid) if inter.guild else None
        except discord.HTTPException:
            membro = None
        if not membro:
            falhas.append(f"<@{uid}> não está neste servidor")
            continue
        if membro.bot:
            falhas.append(f"<@{uid}> é um bot")
            continue
        db.execute("DELETE FROM dm_categorias WHERE dono_id=? AND membro_id=?", (inter.user.id, uid))
        for t in tipos:
            db.execute("INSERT OR IGNORE INTO dm_categorias VALUES(?,?,?)", (inter.user.id, uid, t))
        # Remove registro antigo, para que não restaure 'Anime' ao reiniciar.
        db.execute("DELETE FROM membros WHERE dono_id=? AND membro_id=?", (inter.user.id, uid))
        ok.append(f"<@{uid}> ({', '.join(tipos)})")
        try:
            await membro.send(f"🔔 <@{inter.user.id}> configurou avisos de **{', '.join(tipos)}** "
                              "para você por DM. Use `/sair_dos_avisos` para bloquear novos avisos.",
                              allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            falhas.append(f"<@{uid}> configurado, mas a mensagem inicial não chegou (DM fechada)")
    db.commit()
    return ok, falhas


class DmTiposView(discord.ui.View):
    def __init__(self, usuario: int, ids: list[int]):
        super().__init__(timeout=240)
        self.usuario, self.ids = usuario, ids
        select = discord.ui.Select(placeholder="Selecione uma ou várias categorias de avisos",
           min_values=1, max_values=len(TIPOS_ALERTA),
           options=[discord.SelectOption(label=t, value=t) for t in TIPOS_ALERTA])
        select.callback = self.salvar
        self.add_item(select)

    async def salvar(self, inter: discord.Interaction):
        if inter.user.id != self.usuario:
            return await inter.response.send_message("Somente quem iniciou pode escolher as categorias.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        tipos = inter.data["values"]
        ok, falhas = await registrar_membros(inter, self.ids, tipos)
        texto = ("✅ **Destinatários e categorias:**\n" + "\n".join(ok) if ok else "")
        if falhas:
            texto += "\n⚠️ " + "\n⚠️ ".join(falhas)
        await inter.edit_original_response(content=texto[:1900], view=None)


class DmUsuariosView(discord.ui.View):
    def __init__(self, usuario: int):
        super().__init__(timeout=240)
        self.usuario = usuario
        select = discord.ui.UserSelect(placeholder="Escolha quem receberá os avisos",
                                       min_values=1, max_values=25)
        select.callback = self.avancar
        self.add_item(select)

    async def avancar(self, inter: discord.Interaction):
        if inter.user.id != self.usuario:
            return await inter.response.send_message("Somente quem iniciou pode selecionar pessoas.", ephemeral=True)
        ids = [int(i) for i in inter.data.get("values", [])]
        await inter.response.edit_message(content="Agora escolha **quais categorias** essas pessoas receberão por DM:",
                                          view=DmTiposView(inter.user.id, ids))


@bot.tree.command(name="dm", description="Escolher destinatários e categorias de avisos por DM")
@app_commands.guild_only()
@app_commands.describe(membros="Opcional: @pessoas ou IDs; vazio abre o seletor de usuários")
async def dm(inter: discord.Interaction, membros: Optional[str] = None):
    if not membros:
        return await inter.response.send_message("Selecione os membros que receberão avisos por DM:",
                                                  view=DmUsuariosView(inter.user.id), ephemeral=True)
    ids = extrair_ids(membros)
    if not ids or len(ids) > 25:
        return await inter.response.send_message("Informe de 1 a 25 menções/IDs válidos.", ephemeral=True)
    await inter.response.send_message("Escolha agora as categorias dos avisos:",
                                      view=DmTiposView(inter.user.id, ids), ephemeral=True)


@bot.tree.command(name="dm_lista", description="Mostra os destinatários e suas categorias configuradas")
async def dm_lista(inter: discord.Interaction):
    ids = membros_do_dono(inter.user.id)
    if not ids:
        return await inter.response.send_message("Você não configurou destinatários ainda. Use `/dm`.", ephemeral=True)
    linhas = []
    for n, uid in enumerate(ids, 1):
        cats = [r[0] for r in db.execute("SELECT tipo FROM dm_categorias WHERE dono_id=? AND membro_id=?",
                                         (inter.user.id, uid))]
        linhas.append(f"**{n}.** <@{uid}>: {', '.join(cats)}")
    await inter.response.send_message("\n".join(linhas)[:1900], ephemeral=True)


@bot.tree.command(name="remover_dm", description="Remover destinatários por @, ID ou número da /dm_lista")
@app_commands.describe(itens="Ex.: @fulano, 2, 4-5")
async def remover_dm(inter: discord.Interaction, itens: str):
    ids = membros_do_dono(inter.user.id)
    if not ids:
        return await inter.response.send_message("Sua lista de DMs está vazia.", ephemeral=True)
    remover = {i for i in extrair_ids(itens) if i in ids}
    resto = re.sub(r"<@!?\d+>|\b\d{15,20}\b", " ", itens)
    for tok in re.split(r"[,;\s]+", resto):
        if not tok:
            continue
        faixa = re.fullmatch(r"(\d+)-(\d+)", tok)
        if tok.isdigit() or faixa:
            a, b = sorted((int(faixa[1]), int(faixa[2]))) if faixa else (int(tok), int(tok))
            for pos in range(max(1, a), min(len(ids), b) + 1):
                remover.add(ids[pos-1])
    for uid in remover:
        db.execute("DELETE FROM dm_categorias WHERE dono_id=? AND membro_id=?", (inter.user.id, uid))
        db.execute("DELETE FROM membros WHERE dono_id=? AND membro_id=?", (inter.user.id, uid))
    db.commit()
    await inter.response.send_message("🗑️ Removidos: " + (", ".join(f"<@{u}>" for u in remover) if remover else "nenhum encontrado"), ephemeral=True)


@bot.tree.command(name="sair_dos_avisos", description="Bloquear avisos por DM configurados por outras pessoas")
async def sair_dos_avisos(inter: discord.Interaction):
    db.execute("DELETE FROM dm_categorias WHERE membro_id=?", (inter.user.id,))
    db.execute("DELETE FROM membros WHERE membro_id=?", (inter.user.id,))
    db.execute("INSERT OR IGNORE INTO dm_bloqueios VALUES(?)", (inter.user.id,))
    db.commit()
    await inter.response.send_message("🔕 Você não receberá esses avisos por DM e não poderá ser readicionado até usar `/permitir_dm`.", ephemeral=True)


@bot.tree.command(name="permitir_dm", description="Permitir que membros configurem avisos por DM para você")
async def permitir_dm(inter: discord.Interaction):
    db.execute("DELETE FROM dm_bloqueios WHERE user_id=?", (inter.user.id,))
    db.commit()
    await inter.response.send_message("✅ Agora você pode voltar a ser adicionado a listas de avisos por DM.", ephemeral=True)


# ---------------------------------------------------------------------------
# Novos episódios: datas TMDB e TVmaze; avisos de estreia para filmes.
# ---------------------------------------------------------------------------
def data_utc(ymd: str) -> Optional[datetime]:
    try:
        # Fontes que só trazem dia: anunciar ao meio-dia no fuso do Brasil,
        # SEM afirmar que este é o horário de publicação no streaming.
        return datetime.strptime(ymd, "%Y-%m-%d").replace(hour=12, tzinfo=FUSO_BR).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def ident_ep(season: int, numero: int) -> str:
    return f"S{int(season):02d}E{int(numero):03d}"


async def coletar_tv_tmdb(mid: int) -> list[dict]:
    dados = await tmdb_json(f"/tv/{mid}") or {}
    temporadas = dados.get("number_of_seasons") or 0
    ult = (dados.get("last_episode_to_air") or {}).get("season_number")
    nxt = (dados.get("next_episode_to_air") or {}).get("season_number")
    targets = {x for v in (ult, nxt, temporadas) if isinstance(v, int)
               for x in (v-1, v, v+1) if 1 <= x <= temporadas}
    eps = []
    for n in sorted(targets):
        temporada = await tmdb_json(f"/tv/{mid}/season/{n}") or {}
        for ep in temporada.get("episodes") or []:
            d = data_utc(ep.get("air_date"))
            if d and ep.get("episode_number"):
                eps.append({"key": ident_ep(n, ep["episode_number"]), "quando": d,
                            "numero": ep["episode_number"], "temporada": n,
                            "nome": ep.get("name") or "", "source": "TMDB"})
    return eps


async def coletar_tv_tvmaze(mid: int) -> list[dict]:
    dados = await api_json(f"{TVMAZE_API}/shows/{mid}/episodes") or []
    eps = []
    for ep in dados:
        if not ep.get("number") or not isinstance(ep.get("season"), int):
            continue
        try:
            stamp = ep.get("airstamp")
            d = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc) if stamp else data_utc(ep.get("airdate"))
        except ValueError:
            d = data_utc(ep.get("airdate"))
        if d:
            eps.append({"key": ident_ep(ep.get("season", 0), ep["number"]), "quando": d,
                        "numero": ep["number"], "temporada": ep.get("season", 0),
                        "nome": ep.get("name") or "", "source": "TVmaze"})
    return eps


async def buscar_lancamentos(origem: str, mid: int, tvmaze_id: Optional[int], tipo: str):
    if tipo in TIPOS_FILME:
        movie = await tmdb_json(f"/movie/{mid}") or {}
        d = data_utc(movie.get("release_date"))
        return ([{"key": "ESTREIA", "quando": d, "numero": None,
                 "temporada": None, "nome": movie.get("title", ""), "source": "TMDB"}]
                if d else [])
    epis = {}
    # Prioriza o TVmaze quando fornece data e hora. TMDB cobre lacunas.
    erro_tmdb = None
    if origem == "tmdb":
        try:
            for x in await coletar_tv_tmdb(mid):
                epis[x["key"]] = x
        except Exception as e:
            erro_tmdb = e
    if tvmaze_id or origem == "tvmaze":
        try:
            for x in await coletar_tv_tvmaze(tvmaze_id or mid):
                epis[x["key"]] = x
        except Exception:
            if not epis:
                raise
    if erro_tmdb and not epis:
        raise erro_tmdb
    return list(epis.values())


def registro_ja_entregue(destino: str, ident: int, origem: str, mid: int, ep: str, tipo: str):
    return db.execute("""SELECT 1 FROM alertas_entregues
        WHERE destino=? AND destino_id=? AND origem=? AND mid=? AND episodio=? AND tipo=?""",
        (destino, ident, origem, mid, ep, tipo)).fetchone() is not None


def marcar_entregue(destino: str, ident: int, origem: str, mid: int, ep: str, tipo: str):
    db.execute("INSERT OR IGNORE INTO alertas_entregues VALUES(?,?,?,?,?,?)",
               (destino, ident, origem, mid, ep, tipo))
    db.commit()


def embed_conteudo(titulo: str, tipo: str, plataforma: str, origem: str, mid: int,
                   ep: dict, poster_url: Optional[str] = None):
    filme = tipo in TIPOS_FILME
    tit = f"🎬 Estreia prevista: {titulo}" if filme else f"📺 Novo episódio previsto: {titulo}"
    timestamp = int(ep["quando"].timestamp())
    desc = ("A data pode ser a estreia original e **não confirma disponibilidade** "
            "na plataforma informada, especialmente no Brasil.\n"
            f"📅 Data informada: <t:{timestamp}:D>\n")
    if not filme:
        desc += f"Temporada **{ep['temporada']}**, episódio **{ep['numero']}**"
        if ep.get("nome"):
            desc += f" · {discord.utils.escape_markdown(ep['nome'])[:90]}"
        desc += "\n"
    desc += f"🎞️ Categoria: **{tipo}**\n📺 Plataforma informada pelo membro: **{plataforma}**"
    url = (f"https://www.themoviedb.org/{'movie' if filme else 'tv'}/{mid}"
           if origem == "tmdb" else f"https://www.tvmaze.com/shows/{mid}")
    emb = discord.Embed(title=tit[:250], url=url, description=desc[:4000], color=0x5865F2)
    if poster_url and poster_url.startswith("https://"):
        emb.set_thumbnail(url=poster_url)
    emb.set_footer(text=f"Informações: {ep['source']} · Datas sujeitas a alterações")
    return emb


def botoes_streaming_conteudo(titulo: str, tipo: str, plataforma: str,
                              origem: str, mid: int) -> discord.ui.View:
    """Links legais de consulta/busca, sem afirmar que o título já está no streaming."""
    view = discord.ui.View(timeout=None)
    eh_filme = tipo in TIPOS_FILME
    if origem == "tmdb":
        tipo_tmdb = "movie" if eh_filme else "tv"
        view.add_item(discord.ui.Button(
            label="Onde assistir (Brasil)",
            url=f"https://www.themoviedb.org/{tipo_tmdb}/{mid}/watch?locale=BR"))
    else:
        view.add_item(discord.ui.Button(
            label="Detalhes no TVmaze", url=f"https://www.tvmaze.com/shows/{mid}"))
    # Links de BUSCA: não são links diretos de reprodução ou prova de disponibilidade.
    nome = plataforma.strip()[:55]
    q = quote_plus(titulo)
    pl = normalizar(nome)
    sites = (
        ("netflix", f"https://www.netflix.com/search?q={q}"),
        ("crunchyroll", f"https://www.crunchyroll.com/search?q={q}"),
        ("viki", f"https://www.viki.com/search?q={q}"),
    )
    url_plataforma = next((link for palavra, link in sites if palavra in pl), None)
    if not url_plataforma:
        url_plataforma = "https://www.google.com/search?q=" + quote_plus(f"{titulo} {nome} assistir")
    view.add_item(discord.ui.Button(label=f"Buscar na {nome or 'plataforma'}"[:80],
                                    url=url_plataforma))
    view.add_item(discord.ui.Button(label="Buscar onde assistir",
             url="https://www.google.com/search?q=" + quote_plus(f"{titulo} onde assistir Brasil")))
    return view


def aviso_teste_conteudo(titulo: str, tipo: str, plataforma: str,
                         origem: str, mid: int, ep: dict,
                         poster_url: Optional[str] = None):
    emb = embed_conteudo(titulo, tipo, plataforma, origem, mid, ep, poster_url)
    emb.title = f"🧪 TESTE | {'Estreia' if tipo in TIPOS_FILME else 'Episódio'}: {titulo}"[:256]
    emb.description = ("**Aviso de teste, não é um lançamento neste momento.**\n"
                       + (emb.description or ""))[:4096]
    return emb


async def enviar_teste_conteudo(canal, titulo: str, tipo: str, plataforma: str,
                                origem: str, mid: int, tvmaze_id: Optional[int],
                                poster_url: Optional[str] = None) -> bool:
    """Envia apenas lançamentos que já ocorreram, não marca aviso real como entregue."""
    eps = await buscar_lancamentos(origem, mid, tvmaze_id, tipo)
    agora = datetime.now(timezone.utc)
    anteriores = [ep for ep in eps if ep.get("quando") and ep["quando"] <= agora]
    if not anteriores:
        return False
    ep = max(anteriores, key=lambda e: e["quando"])
    await canal.send(embed=aviso_teste_conteudo(titulo, tipo, plataforma, origem,
                      mid, ep, poster_url),
                     view=botoes_streaming_conteudo(titulo, tipo, plataforma, origem, mid))
    return True


async def validar_canal_teste(inter: discord.Interaction, canal: discord.TextChannel) -> Optional[str]:
    if not inter.guild or canal.guild.id != inter.guild.id:
        return "Use o comando em um servidor e escolha um canal deste servidor."
    perms_usuario = canal.permissions_for(inter.user)
    if not perms_usuario.view_channel or not perms_usuario.send_messages:
        return "Você não tem permissão para enviar mensagens nesse canal."
    membro_bot = inter.guild.me
    if not membro_bot:
        return "Não consegui verificar as permissões do bot no servidor."
    p = canal.permissions_for(membro_bot)
    if not (p.view_channel and p.send_messages and p.embed_links):
        return "O bot precisa das permissões **Ver canal**, **Enviar mensagens** e **Incorporar links** no canal."
    return None


async def despachar_lancamento(origem: str, mid: int, ep: dict, rows: list):
    # rows: (user_id, titulo, tipo, plataforma, criado_em)
    agora = datetime.now(timezone.utc)
    ativos = []
    for uid, titulo, tipo, plataforma, criado in rows:
        dt_follow = datetime.fromtimestamp(criado, timezone.utc)
        # Se o usuário começou a seguir depois do DIA DE LANÇAMENTO, não anunciar retroativamente.
        if dt_follow.astimezone(FUSO_BR).date() > ep["quando"].astimezone(FUSO_BR).date():
            continue
        ativos.append((uid, titulo, tipo, plataforma))
    if not ativos:
        return
    # Envio separado por categoria; não menciona destinatários que não seguem essa categoria.
    por_tipo = {}
    for uid, titulo, tipo, plataforma in ativos:
        por_tipo.setdefault(tipo, []).append((uid, titulo, plataforma))
    for tipo, assinantes in por_tipo.items():
        subs_canais = {}
        subs_dm = set()
        for uid, titulo, plataforma in assinantes:
            for (cid,) in db.execute("""SELECT channel_id FROM canais_conteudo
                        WHERE user_id=? AND tipo=?""", (uid, tipo)):
                subs_canais.setdefault(cid, set()).add(uid)
            for (dest,) in db.execute("""SELECT membro_id FROM dm_categorias
                        WHERE dono_id=? AND tipo=?
                        AND membro_id NOT IN (SELECT user_id FROM dm_bloqueios)""", (uid, tipo)):
                subs_dm.add(dest)
        titulo = assinantes[0][1]
        plataformas = sorted({v[2] for v in assinantes})
        plataforma = ", ".join(plataformas)[:180]
        poster_row = db.execute("""SELECT poster_url FROM conteudos_seguidos
              WHERE origem=? AND mid=? AND tipo=? AND poster_url IS NOT NULL LIMIT 1""",
              (origem, mid, tipo)).fetchone()
        emb = embed_conteudo(titulo, tipo, plataforma, origem, mid, ep,
                             poster_row[0] if poster_row else None)
        for cid, pessoas in subs_canais.items():
            if registro_ja_entregue("canal", cid, origem, mid, ep["key"], tipo):
                continue
            c = bot.get_channel(cid)
            if not c:
                continue
            try:
                await c.send(" ".join(f"<@{u}>" for u in pessoas), embed=emb,
                             view=botoes_streaming_conteudo(titulo, tipo, plataforma, origem, mid),
                             allowed_mentions=discord.AllowedMentions(users=True))
                marcar_entregue("canal", cid, origem, mid, ep["key"], tipo)
            except discord.HTTPException as e:
                print(f"Falha no aviso {tipo} para canal {cid}: {e}")
        for uid in subs_dm:
            if registro_ja_entregue("dm", uid, origem, mid, ep["key"], tipo):
                continue
            try:
                target = await bot.fetch_user(uid)
                await target.send(embed=emb,
                                  view=botoes_streaming_conteudo(titulo, tipo, plataforma, origem, mid))
                marcar_entregue("dm", uid, origem, mid, ep["key"], tipo)
            except discord.HTTPException as e:
                print(f"Falha no aviso {tipo} para DM {uid}: {e}")


@tasks.loop(minutes=30)
async def checar_conteudos():
    agora = datetime.now(timezone.utc)
    desde = agora - timedelta(days=JANELA_ATRASO_DIAS)
    grupos = {}
    for uid, titulo, tipo, plataforma, criado, origem, mid, maze in db.execute("""
      SELECT user_id,titulo,tipo,plataforma,criado_em,origem,mid,tvmaze_id
      FROM conteudos_seguidos WHERE origem IS NOT NULL AND mid IS NOT NULL"""):
        grupos.setdefault((origem, mid, maze, kind(tipo)), []).append((uid, titulo, tipo, plataforma, criado))
    if not grupos:
        return
    erros = 0
    for (origem, mid, maze, _), rows in grupos.items():
        try:
            lancamentos = await buscar_lancamentos(origem, mid, maze, rows[0][2])
            for ep in lancamentos:
                # Os alertas de filmes referem-se à estreia cadastrada no TMDB,
                # não necessariamente à data de entrada em Netflix/Prime etc.
                if ep["quando"] and desde <= ep["quando"] <= agora:
                    await despachar_lancamento(origem, mid, ep, rows)
        except Exception as e:
            erros += 1
            print(f"Consulta de episódios falhou ({origem} #{mid}): {type(e).__name__}: {e}")
        await asyncio.sleep(0.5)
    if erros:
        print(f"Consultas com erro nesta rodada: {erros} de {len(grupos)}")


@checar_conteudos.before_loop
async def esperar_conteudos():
    await bot.wait_until_ready()


@bot.tree.command(name="testar_conteudo", description="Testa o último episódio/estreia de um título da sua lista")
@app_commands.guild_only()
@app_commands.describe(numero="Número mostrado em /meus_conteudos", canal="Canal que receberá o teste (opcional)")
async def testar_conteudo(inter: discord.Interaction, numero: app_commands.Range[int, 1, 100000],
                          canal: Optional[discord.TextChannel] = None):
    rows = db.execute("""SELECT titulo,tipo,plataforma,origem,mid,tvmaze_id,poster_url
                         FROM conteudos_seguidos WHERE user_id=? ORDER BY id""",
                      (inter.user.id,)).fetchall()
    if numero > len(rows):
        return await inter.response.send_message("Número inválido. Veja `/meus_conteudos`.", ephemeral=True)
    titulo, tipo, plat, origem, mid, maze, poster = rows[numero-1]
    if not origem or not mid:
        return await inter.response.send_message("Recadastre este conteúdo em `/seguir_conteudo`.", ephemeral=True)
    destino = canal or inter.channel
    erro = await validar_canal_teste(inter, destino)
    if erro:
        return await inter.response.send_message("⚠️ " + erro, ephemeral=True)
    await inter.response.defer(ephemeral=True)
    try:
        enviou = await enviar_teste_conteudo(destino, titulo, tipo, plat, origem, mid, maze, poster)
    except Exception as e:
        print("Erro no teste de conteúdo:", type(e).__name__, e)
        return await inter.followup.send(f"⚠️ Não consegui consultar ou enviar o teste ({type(e).__name__}).", ephemeral=True)
    if not enviou:
        return await inter.followup.send("Não há episódios/estreias **já lançados** com data registrada para este título.", ephemeral=True)
    await inter.followup.send(f"✅ Teste de **{tipo}** enviado em {destino.mention}, com os botões de streaming.", ephemeral=True)


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
# Biblioteca compartilhada + leitor
# Qualquer membro pode adicionar e ler; o progresso é individual.
# O leitor é efêmero: só quem abriu a leitura vê as páginas.
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


@bot.tree.command(name="biblioteca_add", description="Adiciona um mangá à biblioteca compartilhada (CBZ, ZIP ou PDF)")
@app_commands.describe(
    titulo="Nome do mangá/volume",
    arquivo="Arquivo .cbz, .zip ou .pdf (vale o limite de upload do Discord)",
    link="Link direto para o arquivo (alternativa ao anexo)",
    da_pasta="Arquivo que você colocou na pasta 'entrada' do bot (para arquivos grandes)")
@app_commands.autocomplete(da_pasta=entrada_ac)
async def biblioteca_add(inter: discord.Interaction, titulo: str, arquivo: Optional[discord.Attachment] = None,
                         link: Optional[str] = None, da_pasta: Optional[str] = None):
    if sum(bool(x) for x in (arquivo, link, da_pasta)) != 1:
        return await inter.response.send_message(
            "Informe **uma** origem: o anexo `arquivo`, um `link` **ou** `da_pasta`.", ephemeral=True)
    if da_pasta and not await eh_dono(inter.user):
        return await inter.response.send_message(
            "🚫 Apenas o dono do bot pode usar arquivos da pasta de entrada. Envie um anexo ou link direto.", ephemeral=True)
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


@bot.tree.command(name="biblioteca", description="Mostra a biblioteca compartilhada de mangás (numerada)")
async def biblioteca(inter: discord.Interaction):
    rows = db.execute("SELECT id, titulo, paginas, dono_id FROM biblioteca ORDER BY id").fetchall()
    if not rows:
        return await inter.response.send_message("A biblioteca está vazia. Use `/biblioteca_add` para adicionar um mangá.", ephemeral=True)
    prog = dict(db.execute("SELECT manga_id, pagina FROM progresso WHERE user_id=?", (inter.user.id,)).fetchall())
    linhas = []
    for i, (mid, titulo, total, uploader_id) in enumerate(rows, 1):
        extra = f" • você parou na pág. {prog[mid] + 1}" if mid in prog else ""
        linhas.append(f"**{i}.** {titulo} — {total} págs • enviado por <@{uploader_id}>{extra}")
    await inter.response.send_message("\n".join(linhas)[:1900], ephemeral=True)


@bot.tree.command(name="biblioteca_remover", description="Remove mangás da biblioteca por nome, número ou faixa (ex.: 1, 3-5, naruto)")
@app_commands.describe(itens="Nomes e/ou números da /biblioteca, separados por vírgula")
async def biblioteca_remover(inter: discord.Interaction, itens: str):
    rows = db.execute("SELECT id, titulo, caminho, dono_id FROM biblioteca ORDER BY id").fetchall()
    if not rows:
        return await inter.response.send_message("A biblioteca compartilhada está vazia.", ephemeral=True)

    remover: dict[int, tuple[str, str, int]] = {}
    problemas: list[str] = []
    for item in dividir(itens):
        faixa = re.fullmatch(r"(\d+)\s*-\s*(\d+)", item)
        if item.isdigit() or faixa:
            a, b = sorted((int(faixa[1]), int(faixa[2]))) if faixa else (int(item), int(item))
            if a < 1 or a > len(rows):
                problemas.append(f"nº {item}: não existe na lista")
                continue
            for n in range(a, min(b, len(rows)) + 1):
                row = rows[n - 1]
                if row[3] == inter.user.id:
                    remover[row[0]] = (row[1], row[2], row[3])
                else:
                    problemas.append(f"“{row[1]}”: só quem enviou esse mangá pode removê-lo.")
        else:
            baixo = item.lower()
            achados = [r for r in rows if r[1].lower() == baixo] or [r for r in rows if baixo in r[1].lower()]
            if len(achados) == 1:
                row = achados[0]
                if row[3] == inter.user.id:
                    remover[row[0]] = (row[1], row[2], row[3])
                else:
                    problemas.append(f"“{row[1]}”: só quem enviou esse mangá pode removê-lo.")
            elif not achados:
                problemas.append(f"“{item}”: não encontrado")
            else:
                problemas.append(f"“{item}”: bate com mais de um ({', '.join(r[1] for r in achados[:3])}…). Seja mais específico ou use o número")

    for mid, (_, caminho, _) in remover.items():
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
        partes.append("🗑️ **Removidos (arquivo apagado do bot):**\n" + "\n".join(f"• {t}" for t, _, _ in remover.values()))
        partes.append("Os números mudam depois de remover. Use `/biblioteca` para ver a nova lista.")
    if problemas:
        partes.append("⚠️ **Não consegui:**\n" + "\n".join(f"• {p}" for p in problemas))
    await inter.response.send_message("\n\n".join(partes)[:1900], ephemeral=True)


def resolver_manga(user_id: int, texto: str):
    """Devolve uma entrada da biblioteca compartilhada ou uma mensagem de erro."""
    rows = db.execute("SELECT id, titulo, caminho, paginas FROM biblioteca ORDER BY id").fetchall()
    if not rows:
        return "A biblioteca está vazia. Use `/biblioteca_add` para adicionar um mangá."
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
    rows = db.execute("SELECT id, titulo FROM biblioteca ORDER BY id").fetchall()
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


@bot.tree.command(name="ler", description="Abre um mangá da biblioteca compartilhada (só você vê)")
@app_commands.describe(manga="Título ou número da /biblioteca",
                       pagina="Página inicial (opcional; sem isso, continua de onde você parou)")
@app_commands.autocomplete(manga=biblioteca_ac)
async def ler(inter: discord.Interaction, manga: str, pagina: Optional[int] = None):
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
    """Cria botões oficiais quando existirem; senão, oferece busca de streaming."""
    kwargs = {"embed": emb}
    if com_botoes:
        view = discord.ui.View()
        for l in links[:5]:
            view.add_item(discord.ui.Button(label=f"Assistir na {l['site']}"[:80], url=l["url"]))
        if not links:
            nome_anime = (emb.title or "Anime").split(" — ")[0]
            nome_anime = nome_anime.replace("🧪 TESTE", "").replace("📺", "").strip(" —")
            view.add_item(discord.ui.Button(label="Buscar onde assistir", url=
                "https://www.google.com/search?q=" + quote_plus(f"{nome_anime} assistir anime oficialmente")))
        kwargs["view"] = view
    return kwargs


async def enviar(ep: dict, lembrete: bool):
    m = ep["media"]
    emb, links = montar_aviso(ep, "lembrete" if lembrete else "lancamento")
    botoes = not lembrete

    canais: dict[int, set[int]] = {}
    dms: set[int] = set()
    for (dono,) in db.execute("SELECT user_id FROM follows WHERE media_id=?", (m["id"],)):
        dests = db.execute("""SELECT channel_id FROM canais WHERE user_id=?
                             UNION SELECT channel_id FROM canais_conteudo
                             WHERE user_id=? AND tipo='Anime'""", (dono, dono)).fetchall()
        for (cid,) in dests:
            canais.setdefault(cid, set()).add(dono)
        for (uid,) in db.execute("""SELECT membro_id FROM dm_categorias
                                    WHERE dono_id=? AND tipo='Anime'
                                    AND membro_id NOT IN (SELECT user_id FROM dm_bloqueios)""", (dono,)):
            dms.add(uid)

    for cid, donos in canais.items():
        canal_obj = bot.get_channel(cid)
        if not canal_obj:
            continue
        try:
            await canal_obj.send(" ".join(f"<@{u}>" for u in donos),
                                 allowed_mentions=discord.AllowedMentions(users=True),
                                 **kw_aviso(emb, links, botoes))
        except discord.HTTPException as e:
            print(f"Falha ao avisar no canal {cid}: {e}")

    for uid in dms:
        try:
            user = await bot.fetch_user(uid)
            await user.send(**kw_aviso(emb, links, botoes))
        except discord.HTTPException as e:
            print(f"Falha ao enviar DM para {uid}: {e}")


# ---------------------------------------------------------------------------
# /testar: botão para testar qualquer categoria num canal escolhido.
# O aviso de teste não marca um lançamento real como entregue.
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
    def __init__(self, usuario_id: int, categoria: str, canal_id: int,
                 media_id: Optional[int] = None):
        super().__init__(timeout=300)
        self.usuario_id = usuario_id
        self.categoria = categoria
        self.canal_id = canal_id
        self.media_id = media_id
        self.enviado = False

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.usuario_id:
            await inter.response.send_message("Somente quem usou `/testar` pode clicar neste botão.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Enviar aviso de teste", emoji="🧪", style=discord.ButtonStyle.primary)
    async def enviar_teste(self, inter: discord.Interaction, button: discord.ui.Button):
        if self.enviado:
            return await inter.response.send_message("O teste já foi enviado. Use `/testar` novamente.", ephemeral=True)
        destino = inter.guild.get_channel(self.canal_id) if inter.guild else None
        if not destino:
            return await inter.response.send_message("O canal de teste não está mais disponível.", ephemeral=True)
        erro = await validar_canal_teste(inter, destino)
        if erro:
            return await inter.response.send_message("⚠️ " + erro, ephemeral=True)
        self.enviado = True
        await inter.response.defer(ephemeral=True)
        try:
            melhor = None  # (data_lancamento, titulo, ep, row/conteudo)
            if self.categoria == "Anime":
                ids = ([self.media_id] if self.media_id else [r[0] for r in db.execute(
                    "SELECT media_id FROM follows WHERE user_id=?", (inter.user.id,))])
                for mid in ids:
                    try:
                        ep = await ultimo_episodio(mid)
                        if ep and (melhor is None or ep["airingAt"] > melhor[0]):
                            melhor = (ep["airingAt"], "anime", ep)
                    except Exception as e:
                        print("Erro ao consultar anime para teste:", type(e).__name__)
            if not self.media_id:
                rows = db.execute("""SELECT titulo,tipo,plataforma,origem,mid,tvmaze_id,poster_url
                    FROM conteudos_seguidos WHERE user_id=? AND tipo=? AND origem IS NOT NULL AND mid IS NOT NULL""",
                    (inter.user.id, self.categoria)).fetchall()
                now = datetime.now(timezone.utc)
                for titulo, tipo, plat, origem, mid, maze, poster in rows:
                    try:
                        episodes = await buscar_lancamentos(origem, mid, maze, tipo)
                        anteriores = [ep for ep in episodes if ep.get("quando") and ep["quando"] <= now]
                        if anteriores:
                            ultimo = max(anteriores, key=lambda e: e["quando"])
                            stamp = ultimo["quando"].timestamp()
                            if melhor is None or stamp > melhor[0]:
                                melhor = (stamp, "conteudo", (titulo, tipo, plat, origem, mid, poster, ultimo))
                    except Exception as e:
                        print("Erro ao consultar conteúdo para teste:", type(e).__name__)
            if not melhor:
                self.enviado = False
                return await inter.followup.send(
                    f"Nenhum lançamento anterior à data atual foi encontrado para **{self.categoria}**. "
                    "Cadastre obras com `/seguir` ou `/seguir_conteudo` e tente novamente.", ephemeral=True)
            if melhor[1] == "anime":
                emb, links = montar_aviso(melhor[2], "teste")
                await destino.send(**kw_aviso(emb, links, True))
            else:
                titulo, tipo, plat, origem, mid, poster, ep = melhor[2]
                await destino.send(embed=aviso_teste_conteudo(titulo, tipo, plat, origem, mid, ep, poster),
                    view=botoes_streaming_conteudo(titulo, tipo, plat, origem, mid))
            button.disabled = True
            await inter.edit_original_response(view=self)
            await inter.followup.send(f"✅ Teste de **{self.categoria}** enviado em {destino.mention}.", ephemeral=True)
        except discord.HTTPException as e:
            self.enviado = False
            await inter.followup.send(f"⚠️ Falha ao enviar o teste no Discord: {e}", ephemeral=True)
        except Exception as e:
            self.enviado = False
            print("Erro no teste:", type(e).__name__, e)
            await inter.followup.send(f"⚠️ Não foi possível preparar o teste: {type(e).__name__}.", ephemeral=True)


@bot.tree.command(name="testar", description="Testar aviso de anime, dorama, K-drama, série ou filme com botões")
@app_commands.guild_only()
@app_commands.describe(anime="Anime específico de /seguir (opcional)",
                       categoria="Tipo de conteúdo (padrão: Anime)",
                       canal="Canal que receberá o teste (padrão: canal atual)")
@app_commands.choices(categoria=TIPOS_CONTEUDO)
@app_commands.autocomplete(anime=seguindo_ac)
async def testar(inter: discord.Interaction, anime: Optional[str] = None,
                 categoria: Optional[app_commands.Choice[str]] = None,
                 canal: Optional[discord.TextChannel] = None):
    tipo = categoria.value if categoria else "Anime"
    if anime and tipo != "Anime":
        return await inter.response.send_message("Escolha a categoria Anime ao informar um anime específico.", ephemeral=True)
    if anime and not anime.isdigit():
        return await inter.response.send_message("Selecione um anime da lista de sugestões.", ephemeral=True)
    destino = canal or inter.channel
    erro = await validar_canal_teste(inter, destino)
    if erro:
        return await inter.response.send_message("⚠️ " + erro, ephemeral=True)
    await inter.response.send_message(
        f"🧪 **Teste de aviso: {tipo}**\nDestino: {destino.mention}\n"
        "O bot usará o **lançamento já ocorrido mais recente** das obras que você acompanha "
        "nessa categoria e enviará uma prévia com os botões disponíveis.\n\n"
        "**Atenção:** os botões para séries/filmes são links de consulta ou busca; "
        "não garantem que a obra esteja disponível no streaming informado.",
        view=TesteView(inter.user.id, tipo, destino.id, int(anime) if anime else None), ephemeral=True)


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
