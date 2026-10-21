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
from urllib.parse import quote_plus, urlsplit, parse_qs
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
db.executescript("""
    CREATE TABLE IF NOT EXISTS links_temporadas(
        user_id INTEGER NOT NULL, origem TEXT NOT NULL, mid INTEGER NOT NULL,
        temporada INTEGER NOT NULL DEFAULT 0, url TEXT NOT NULL,
        PRIMARY KEY (user_id, origem, mid, temporada));
    CREATE TABLE IF NOT EXISTS anime_temporada_ativa(
        user_id INTEGER NOT NULL, media_id INTEGER NOT NULL,
        temporada INTEGER NOT NULL,
        PRIMARY KEY (user_id, media_id));
""")
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
        comandos = await self.tree.sync()
        print(f"[Shiro] {len(comandos)} comandos globais sincronizados: "
              + ", ".join(sorted(c.name for c in comandos)))
        # Opcional: servidor de testes ganha slash commands imediatamente.
        gid = os.getenv("DISCORD_GUILD_ID", "").strip()
        if gid.isdigit():
            guild = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            comandos_guild = await self.tree.sync(guild=guild)
            print(f"[Shiro] {len(comandos_guild)} comandos sincronizados no servidor {gid}.")
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


# Links opcionais cadastrados pelos membros. 0 = link geral da obra;
# 1,2,3... = link da temporada correspondente.
# O link identifica onde consultar o conteúdo, não é evidência de lançamento.
def normalizar_link_plataforma(url: str) -> str:
    url = (url or "").strip().strip("<>").replace("\\&", "&")
    validado = url_legal_externa(url)
    if not validado:
        raise ValueError("Informe um link HTTPS público e válido (sem espaços ou credenciais).")
    parsed = urlsplit(validado)
    host = (parsed.hostname or "").lower()
    if host == "netflix.com" or host.endswith(".netflix.com"):
        # URLs de pesquisa com jbv=12345678 não são páginas estáveis para acompanhar.
        # Extrai o ID compartilhado pela Netflix e cria a URL oficial do título.
        jbv = (parse_qs(parsed.query).get("jbv") or [None])[0]
        original = re.search(r"/(?:title|watch)/(\d{6,12})(?:/|$)", parsed.path)
        netflix_id = original.group(1) if original else jbv
        if netflix_id and re.fullmatch(r"\d{6,12}", netflix_id):
            return f"https://www.netflix.com/br/title/{netflix_id}"
        if parsed.path.startswith("/search"):
            raise ValueError("Na Netflix, envie o link do título (/title/123...) ou um link que tenha jbv=123...")
    return validado


def ler_links_opcionais(link: Optional[str] = None, temporada: Optional[int] = None,
                        temporadas: Optional[str] = None) -> dict[int, str]:
    """Aceita link simples e/ou 'T1: https://...; T2: https://...; Geral: ...'."""
    saida: dict[int, str] = {}
    if temporada is not None and (temporada < 1 or temporada > 100):
        raise ValueError("Informe temporada de 1 a 100.")
    if temporada is not None and not link:
        raise ValueError("Informe o link junto do número da temporada.")
    if link:
        saida[temporada or 0] = normalizar_link_plataforma(link)
    if temporadas:
        partes = [x.strip() for x in re.split(r"[;\n]+", temporadas) if x.strip()]
        if len(partes) > 15:
            raise ValueError("Cadastre no máximo 15 links de temporadas por comando.")
        for parte in partes:
            m = re.fullmatch(r"(?i)(?:T(?:emporada)?\s*0*(\d{1,3})|(geral|base|obra))\s*:\s*(https://\S+)", parte)
            if not m:
                raise ValueError("Formato inválido. Ex.: T1: https://site/1; T2: https://site/2")
            num = int(m.group(1)) if m.group(1) else 0
            if num > 100 or (m.group(1) and num < 1):
                raise ValueError("A temporada precisa ser de T1 a T100.")
            saida[num] = normalizar_link_plataforma(m.group(3))
    if len(saida) > 15:
        raise ValueError("Cadastre no máximo 15 links por vez.")
    return saida


def gravar_links(user_id: int, origem: str, mid: int, links: dict[int, str]):
    for numero, url in links.items():
        db.execute("""INSERT OR REPLACE INTO links_temporadas
                     (user_id,origem,mid,temporada,url) VALUES (?,?,?,?,?)""",
                   (user_id, origem, int(mid), int(numero), url))
    db.commit()


def links_de_usuarios(usuarios: list[int], origem: str, mid: int,
                      temporada: Optional[int] = None) -> list[tuple[int, str]]:
    """A temporada exata ganha prioridade; o geral é o fallback.
    Quando não existe identificação de temporada (caso do AniList), não supõe
    que um link T3 seja da T1: admite um único link específico ou o geral.
    """
    if not usuarios:
        return []
    usuarios = list(dict.fromkeys(int(u) for u in usuarios))[:150]
    placeholders = ",".join("?" for _ in usuarios)
    if origem == "anilist" and temporada is None:
        preferencias = db.execute(f"""SELECT DISTINCT temporada FROM anime_temporada_ativa
                 WHERE media_id=? AND user_id IN ({placeholders})""",
                 (int(mid), *usuarios)).fetchall()
        if len(preferencias) == 1:
            temporada = int(preferencias[0][0])
    rows = db.execute(f"""SELECT temporada,url FROM links_temporadas
                    WHERE origem=? AND mid=? AND user_id IN ({placeholders})""",
                    (origem, int(mid), *usuarios)).fetchall()
    if not rows:
        return []
    por_temporada: dict[int, list[str]] = {}
    for num, url in rows:
        por_temporada.setdefault(int(num), [])
        if url not in por_temporada[num]:
            por_temporada[num].append(url)
    ordem: list[int] = []
    if temporada is not None and int(temporada) in por_temporada:
        ordem.append(int(temporada))
    ordem.append(0)
    if temporada is None and len([x for x in por_temporada if x > 0]) == 1:
        ordem.extend(x for x in por_temporada if x > 0)
    resultado = []
    for n in ordem:
        for url in por_temporada.get(n, []):
            if url not in [v for _, v in resultado]:
                resultado.append((n, url))
            if len(resultado) == 2:
                return resultado
    return resultado


def botoes_links_usuario(view: discord.ui.View, links: list[tuple[int, str]],
                          urls: set[str], limite: int = 5):
    for num, endereco in links:
        valido = url_legal_externa(endereco)
        if not valido or valido in urls or len(view.children) >= limite:
            continue
        dominio = (urlsplit(valido).hostname or "plataforma").removeprefix("www.")
        if num:
            rotulo = f"▶️ Abrir T{num} em {dominio}"
        else:
            rotulo = f"▶️ Abrir obra em {dominio}"
        # Não chama de 'assistir episódio exato': a URL é da obra/temporada.
        view.add_item(discord.ui.Button(label=rotulo[:80], url=valido))
        urls.add(valido)


# Busca unificada para /seguir, /seguir_conteudo e /seguir_varios.
# TVmaze funciona sem chave; TMDB necessita de TMDB_API_KEY.
async def resultados_por_categoria(titulo: str, categoria: str, limite: int = 10):
    resultados = []
    if TMDB_API_KEY:
        try:
            dados = await tmdb_json(f"/search/{kind(categoria)}", {
                "query": titulo, "include_adult": "false"}) or {}
            resultados.extend(("tmdb", r) for r in dados.get("results", [])[:limite])
        except Exception as exc:
            print("Erro na pesquisa TMDB:", type(exc).__name__, str(exc))
    if categoria in TIPOS_TV and len(resultados) < min(5, limite):
        try:
            dados = await api_json(f"{TVMAZE_API}/search/shows", {"q": titulo}) or []
            resultados.extend(("tvmaze", x["show"]) for x in dados[:limite-len(resultados)]
                              if x.get("show"))
        except Exception as exc:
            print("Erro na pesquisa TVmaze:", type(exc).__name__, str(exc))
    return resultados[:limite]


async def autocomplete_todos_titulos(inter: discord.Interaction, atual: str):
    """Após selecionar a categoria, lista resultados das APIs correspondentes."""
    categoria = getattr(inter.namespace, "categoria", None)
    categoria = getattr(categoria, "value", categoria) or "Anime"
    if len(atual.strip()) < 2:
        return []
    try:
        if categoria == "Anime":
            q = """query($s:String){Page(perPage:20){media(search:$s,type:ANIME,sort:SEARCH_MATCH){
                id title{romaji english}}}}"""
            dados = await anilist(bot.session, q, {"s": atual})
            return [app_commands.Choice(name=nome(x["title"])[:100], value=f"al:{x['id']}")
                    for x in (dados.get("Page") or {}).get("media", [])][:25]
        resultados = await resultados_por_categoria(atual, categoria, 15)
        saida = []
        for origem, r in resultados:
            titulo = r.get("name") or r.get("title") or "Sem título"
            ano = (r.get("first_air_date") or r.get("release_date") or
                   r.get("premiered") or "????")[:4]
            saida.append(app_commands.Choice(
                name=f"{titulo[:78]} ({ano}) · {origem.upper()}"[:100],
                value=f"{origem}:{r['id']}"))
        return saida[:25]
    except Exception as exc:
        print("Falha no autocomplete /seguir:", type(exc).__name__)
        return []


@bot.tree.command(name="seguir", description="Seguir anime, dorama, K-drama, série, filme e outras categorias")
@app_commands.describe(categoria="Tipo de obra", titulo="Pesquise e escolha o título correto",
                       plataforma="Streaming opcional (ex.: Crunchyroll, Netflix)",
                       link="Link opcional da obra ou temporada",
                       temporada="Temporada à qual o link corresponde (opcional)",
                       temporadas="Ex.: T1: URL; T2: URL; T3: URL")
@app_commands.choices(categoria=[app_commands.Choice(name=t, value=t) for t in TIPOS_ALERTA])
@app_commands.autocomplete(titulo=autocomplete_todos_titulos)
async def seguir(inter: discord.Interaction, categoria: app_commands.Choice[str],
                titulo: str, plataforma: Optional[str] = None, link: Optional[str] = None,
                temporada: Optional[app_commands.Range[int, 1, 100]] = None,
                temporadas: Optional[str] = None):
    tipo = categoria.value
    try:
        fontes = ler_links_opcionais(link, temporada, temporadas)
    except ValueError as exc:
        return await inter.response.send_message(f"⚠️ {exc}", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    if tipo == "Anime":
        # Anime continua usando AniList, sem depender de chave TMDB.
        try:
            if re.fullmatch(r"al:\d+", titulo):
                mid = int(titulo.split(":", 1)[1])
                q = "query($id:Int){Media(id:$id,type:ANIME){id title{romaji english}}}"
                media = (await anilist(bot.session, q, {"id": mid})).get("Media")
            else:
                media = await buscar_anime(titulo)
            if not media:
                return await inter.followup.send("Anime não encontrado no AniList.", ephemeral=True)
            mid = int(media["id"])
            titulo_certo = nome(media["title"])
            db.execute("INSERT OR REPLACE INTO follows VALUES(?,?,?)",
                       (inter.user.id, mid, titulo_certo))
            if fontes:
                gravar_links(inter.user.id, "anilist", mid, fontes)
            if temporada:
                db.execute("INSERT OR REPLACE INTO anime_temporada_ativa VALUES(?,?,?)",
                           (inter.user.id, mid, temporada))
            db.commit()
            return await inter.followup.send(
                f"✅ Agora você segue **{discord.utils.escape_markdown(titulo_certo)}** (Anime)."
                + (f" 🔗 {len(fontes)} link(s) salvo(s)." if fontes else "")
                + " Configure `/canal` para avisos de animes.", ephemeral=True)
        except Exception as exc:
            print("Erro ao cadastrar anime:", type(exc).__name__, str(exc))
            return await inter.followup.send("⚠️ Não consegui consultar o AniList agora.", ephemeral=True)

    plataforma = (plataforma or "Não informada").strip()[:100]
    # Quando o Discord retorna uma sugestão da API, consulta o ID exato.
    resultados = []
    selecionado = re.fullmatch(r"(tmdb|tvmaze):(\d+)", titulo)
    if selecionado:
        origem, mid = selecionado.group(1), int(selecionado.group(2))
        try:
            if origem == "tmdb":
                r = await tmdb_json(f"/{kind(tipo)}/{mid}")
            elif tipo in TIPOS_TV:
                r = await api_json(f"{TVMAZE_API}/shows/{mid}")
            else:
                r = None
            if r and r.get("id"):
                resultados = [(origem, r)]
        except Exception as exc:
            print("Erro ao consultar título escolhido:", type(exc).__name__, str(exc))
    else:
        resultados = await resultados_por_categoria(titulo.strip(), tipo)
    if not resultados:
        return await inter.followup.send(
            "Não encontrei essa obra. Confira o título e tente novamente."
            + (" Configure `TMDB_API_KEY` na FadeHost." if not TMDB_API_KEY else ""),
            ephemeral=True)
    await inter.followup.send("🔎 **Confirme a obra correta para acompanhar:**",
        view=ResultadosConteudo(inter.user.id, titulo, tipo, plataforma,
                                resultados[:25], fontes), ephemeral=True)


async def seguindo_ac(inter: discord.Interaction, atual: str):
    rows = db.execute("SELECT media_id, title FROM follows WHERE user_id=?", (inter.user.id,)).fetchall()
    return [
        app_commands.Choice(name=t[:100], value=str(i))
        for i, t in rows if atual.lower() in t.lower()
    ][:25]


@bot.tree.command(name="deixar", description="Parar de seguir um anime")
@app_commands.autocomplete(anime=seguindo_ac)
async def deixar(inter: discord.Interaction, anime: str):
    if not anime.isdigit():
        return await inter.response.send_message("Selecione um anime das sugestões.", ephemeral=True)
    db.execute("DELETE FROM follows WHERE user_id=? AND media_id=?", (inter.user.id, int(anime)))
    db.execute("DELETE FROM links_temporadas WHERE user_id=? AND origem='anilist' AND mid=?",
               (inter.user.id, int(anime)))
    db.execute("DELETE FROM anime_temporada_ativa WHERE user_id=? AND media_id=?",
               (inter.user.id, int(anime)))
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
# Acompanhamento e remoção em lote para todas as categorias, inclusive animes.
# A lista é unificada com as obras de /seguir e /seguir_conteudo.
# ---------------------------------------------------------------------------
def dividir(texto: str) -> list[str]:
    """Separa títulos por vírgula, ponto e vírgula ou quebra de linha."""
    return [p.strip() for p in re.split(r"[,;\n]+", texto) if p.strip()]


async def buscar_anime(texto: str) -> Optional[dict]:
    q = """query($s:String){Media(search:$s,type:ANIME,sort:SEARCH_MATCH){id title{romaji english}}}"""
    try:
        return (await anilist(bot.session, q, {"s": texto}))["Media"]
    except Exception as exc:
        print("Falha na pesquisa AniList:", type(exc).__name__)
        return None


def categoria_de_prefixo(nome_categoria: str) -> Optional[str]:
    """Converte prefixos como 'Série:' ou 'K-drama:' na categoria do Discord."""
    def limpar(texto):
        return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().casefold().strip()
    return next((t for t in TIPOS_ALERTA if limpar(t) == limpar(nome_categoria)), None)


def identificar_item_lote(item: str, padrao: str) -> tuple[str, str]:
    pref = re.match(r"^([^:]{2,28}):\s*(.+)$", item)
    if pref:
        categoria = categoria_de_prefixo(pref.group(1))
        if categoria:
            return categoria, pref.group(2).strip()
    return padrao, item.strip()


async def pesquisar_titulo_exato(titulo: str, categoria: str) -> Optional[tuple[str, dict]]:
    """Não associa automaticamente homônimos: exige correspondência exata.

    Para casos ambíguos, o usuário usa /seguir, que mostra uma lista de confirmação.
    """
    resultados = await resultados_por_categoria(titulo, categoria, 15)
    candidatos = []
    alvo = normalizar(titulo)
    for origem, r in resultados:
        nomes = (r.get("name"), r.get("title"), r.get("original_name"),
                 r.get("original_title"))
        if any(normalizar(n or "") == alvo for n in nomes):
            candidatos.append((origem, r))
    # TMDB e TVmaze podem listar a MESMA obra; prefira TMDB quando houver
    # exatamente um resultado com o mesmo título. Se houver homônimos,
    # não escolha um ano aleatório: peça confirmação no /seguir.
    for fonte in ("tmdb", "tvmaze"):
        unicos = {int(r["id"]): (origem, r) for origem, r in candidatos if origem == fonte}
        if len(unicos) == 1:
            return next(iter(unicos.values()))
        if len(unicos) > 1:
            return None
    return None


async def salvar_item_lote(user_id: int, categoria: str, titulo: str,
                           plataforma: str) -> tuple[bool, str]:
    if categoria == "Anime":
        media = await buscar_anime(titulo)
        if not media:
            return False, "não encontrado no AniList"
        encontrado = nome(media["title"])
        # Animes costumam ter título traduzido; avisamos qual foi associado.
        mid = int(media["id"])
        existe = db.execute("SELECT 1 FROM follows WHERE user_id=? AND media_id=?",
                            (user_id, mid)).fetchone()
        db.execute("INSERT OR IGNORE INTO follows VALUES(?,?,?)",
                   (user_id, mid, encontrado))
        return True, f"{encontrado} (Anime)" + (" · já seguia" if existe else "")
    achado = await pesquisar_titulo_exato(titulo, categoria)
    if not achado:
        return False, "nome ambíguo ou não encontrado; use /seguir para confirmar"
    origem, media = achado
    mid = int(media["id"])
    nome_certo = media.get("name") or media.get("title") or titulo
    ano = media.get("first_air_date") or media.get("release_date") or media.get("premiered")
    tvmaze_id = mid if origem == "tvmaze" else None
    imagem = (media.get("image") or {}).get("medium") if origem == "tvmaze" else (
        "https://image.tmdb.org/t/p/w500" + media["poster_path"]
        if media.get("poster_path") else None)
    existe = db.execute("""SELECT id FROM conteudos_seguidos
              WHERE user_id=? AND origem=? AND mid=? AND tipo=?""",
              (user_id, origem, mid, categoria)).fetchone()
    if existe:
        return True, f"{nome_certo} ({categoria}) · já seguia"
    try:
        db.execute("""INSERT INTO conteudos_seguidos
             (user_id,titulo,tipo,plataforma,criado_em,origem,mid,tvmaze_id,poster_url)
             VALUES(?,?,?,?,?,?,?,?,?)""",
             (user_id, nome_certo, categoria, plataforma, int(time.time()),
              origem, mid, tvmaze_id, imagem))
    except sqlite3.IntegrityError:
        return True, f"{nome_certo} ({categoria}) · já seguia"
    # TVmaze tenta vincular a temporada da série em verificações posteriores.
    return True, f"{nome_certo} ({categoria})" + (f" · {str(ano)[:4]}" if ano else "")


@bot.tree.command(name="seguir_varios", description="Seguir várias obras de qualquer categoria de uma vez")
@app_commands.describe(nomes="Ex.: Anime: Frieren; Série: Dark; K-drama: Queen of Tears",
                       categoria="Categoria padrão dos títulos sem prefixo",
                       plataforma="Streaming de referência (opcional)")
@app_commands.choices(categoria=[app_commands.Choice(name=x, value=x) for x in TIPOS_ALERTA])
async def seguir_varios(inter: discord.Interaction, nomes: str,
                        categoria: Optional[app_commands.Choice[str]] = None,
                        plataforma: Optional[str] = None):
    itens = dividir(nomes)
    if not itens:
        return await inter.response.send_message("Informe pelo menos um título.", ephemeral=True)
    if len(itens) > 15:
        return await inter.response.send_message("Máximo de 15 títulos por comando.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    padrao = categoria.value if categoria else "Anime"
    plataforma = (plataforma or "Não informada").strip()[:100]
    ok, falhas = [], []
    for bruto in itens:
        tipo, titulo = identificar_item_lote(bruto, padrao)
        try:
            cadastrado, msg = await salvar_item_lote(inter.user.id, tipo, titulo, plataforma)
            (ok if cadastrado else falhas).append(f"• {titulo} → {msg}")
        except Exception as exc:
            print(f"Falha ao cadastrar em lote ({tipo}):", type(exc).__name__, str(exc))
            falhas.append(f"• {titulo} ({tipo}) → falha na API")
        await asyncio.sleep(0.35)
    db.commit()
    linhas = (["✅ **Cadastrados / já acompanhados:**", *ok] if ok else [])
    if falhas:
        linhas.extend(["", "⚠️ **Precisam de confirmação:**", *falhas])
    linhas.extend(["", "🔗 Agora os títulos aparecem em `/links_temporadas`, "
                   "`/link_temporada`, `/ver_links` e `/remover_link`.",
                   "Use `/lista` para ver números; escolha canais em `/canal` e `/canal_conteudo`."])
    await inter.followup.send("\n".join(linhas)[:1900], ephemeral=True)


def lista_unificada(usuario: int) -> list[dict]:
    """A mesma ordem é utilizada por /lista e /remover_varios."""
    return sorted(titulos_seguidos_teste(usuario),
                  key=lambda x: (x["titulo"].casefold(), x["tipo"], x["ref"]))


@bot.tree.command(name="lista", description="Lista numerada de todas as obras que você acompanha")
async def lista(inter: discord.Interaction):
    itens = lista_unificada(inter.user.id)
    if not itens:
        return await inter.response.send_message("Nenhuma obra seguida. Use `/seguir` ou `/seguir_varios`.", ephemeral=True)
    linhas = [f"**{i}.** {discord.utils.escape_markdown(x['titulo'])} · {x['tipo']}"
              for i, x in enumerate(itens, 1)]
    await inter.response.send_message("\n".join(linhas)[:1900], ephemeral=True)


def apagar_seguido(usuario: int, ref: str):
    """Apaga obra e os links dela apenas se não forem compartilhados por outra entrada."""
    if ref.startswith("f:"):
        mid = int(ref[2:])
        db.execute("DELETE FROM follows WHERE user_id=? AND media_id=?", (usuario, mid))
        db.execute("DELETE FROM anime_temporada_ativa WHERE user_id=? AND media_id=?", (usuario, mid))
        origem = "anilist"
    else:
        id_conteudo = int(ref[2:])
        registro = db.execute("SELECT origem,mid FROM conteudos_seguidos WHERE user_id=? AND id=?",
                              (usuario, id_conteudo)).fetchone()
        if not registro:
            return
        origem, mid = registro
        db.execute("DELETE FROM conteudos_seguidos WHERE user_id=? AND id=?", (usuario, id_conteudo))
    if not origem or not mid:
        return
    sobrou = db.execute("""SELECT 1 FROM conteudos_seguidos
       WHERE user_id=? AND origem=? AND mid=? LIMIT 1""", (usuario, origem, mid)).fetchone()
    if origem == "anilist":
        sobrou = sobrou or db.execute("SELECT 1 FROM follows WHERE user_id=? AND media_id=?",
                                    (usuario, mid)).fetchone()
    if not sobrou:
        db.execute("DELETE FROM links_temporadas WHERE user_id=? AND origem=? AND mid=?",
                   (usuario, origem, mid))


class MenuRemoverVarios(discord.ui.View):
    """Seleção paginada de títulos de todas as categorias, com confirmação."""
    def __init__(self, usuario_id: int):
        super().__init__(timeout=300)
        self.usuario_id = usuario_id
        self.itens = lista_unificada(usuario_id)
        self.pagina = 0
        self.selecionados: set[str] = set()
        self.atualizar_componentes()

    def descricao(self):
        total_paginas = max(1, (len(self.itens) + 24) // 25)
        return (f"🗑️ **Selecione as obras para remover** (página {self.pagina+1}/{total_paginas}).\n"
                f"Você pode marcar títulos de várias categorias e navegar entre páginas.\n"
                f"**Selecionados: {len(self.selecionados)}** · clique em **Confirmar remoção** ao terminar.")

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.usuario_id:
            await inter.response.send_message("Este menu pertence a outro usuário.", ephemeral=True)
            return False
        return True

    def atualizar_componentes(self):
        self.clear_items()
        ini = self.pagina * 25
        atual = self.itens[ini:ini+25]
        options = [discord.SelectOption(
            label=f"{ini+i}. {x['titulo']}"[:100],
            value=x["ref"], description=x["tipo"][:100],
            default=x["ref"] in self.selecionados)
            for i, x in enumerate(atual, 1)]
        seletor = discord.ui.Select(placeholder="Marque uma ou várias obras desta página",
                                   min_values=1, max_values=len(options), options=options)

        async def selecionar(inter: discord.Interaction):
            # Deselecting on this page removes from the pending selection.
            refs_pagina = {x["ref"] for x in atual}
            self.selecionados.difference_update(refs_pagina)
            self.selecionados.update(seletor.values)
            self.atualizar_componentes()
            await inter.response.edit_message(content=self.descricao(), view=self)
        seletor.callback = selecionar
        self.add_item(seletor)
        anterior = discord.ui.Button(label="Anterior", emoji="⬅️", style=discord.ButtonStyle.secondary,
                                     disabled=self.pagina == 0)
        proximo = discord.ui.Button(label="Próxima", emoji="➡️", style=discord.ButtonStyle.secondary,
                                    disabled=(self.pagina+1)*25 >= len(self.itens))
        confirmar = discord.ui.Button(label="Confirmar remoção", emoji="🗑️", style=discord.ButtonStyle.danger)
        cancelar = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)

        async def voltar(inter: discord.Interaction):
            self.pagina -= 1
            self.atualizar_componentes()
            await inter.response.edit_message(content=self.descricao(), view=self)
        async def avancar(inter: discord.Interaction):
            self.pagina += 1
            self.atualizar_componentes()
            await inter.response.edit_message(content=self.descricao(), view=self)
        async def apagar(inter: discord.Interaction):
            if not self.selecionados:
                return await inter.response.send_message("Marque pelo menos uma obra.", ephemeral=True)
            retirados = [x for x in self.itens if x["ref"] in self.selecionados]
            for x in retirados:
                apagar_seguido(self.usuario_id, x["ref"])
            db.commit()
            self.stop()
            resumo = ", ".join(f"{x['titulo']} ({x['tipo']})" for x in retirados)
            await inter.response.edit_message(content=f"✅ Removidas {len(retirados)} obras: {resumo}"[:1900], view=None)
        async def encerrar(inter: discord.Interaction):
            self.stop()
            await inter.response.edit_message(content="Remoção cancelada. Nenhuma obra foi excluída.", view=None)
        anterior.callback = voltar
        proximo.callback = avancar
        confirmar.callback = apagar
        cancelar.callback = encerrar
        for botão in (anterior, proximo, confirmar, cancelar):
            self.add_item(botão)


@bot.tree.command(name="remover_varios", description="Remover várias obras de todas as categorias por nome ou número")
@app_commands.describe(itens="Opcional: 1, 3, 5-7, Dark, Frieren. Vazio abre menu para marcar obras")
async def remover_varios(inter: discord.Interaction, itens: Optional[str] = None):
    rows = lista_unificada(inter.user.id)
    if not rows:
        return await inter.response.send_message("Sua lista está vazia.", ephemeral=True)
    if not itens or not itens.strip():
        view = MenuRemoverVarios(inter.user.id)
        return await inter.response.send_message(view.descricao(), view=view, ephemeral=True)
    remover = {}
    problemas = []
    for item in dividir(itens):
        faixa = re.fullmatch(r"(\d+)\s*-\s*(\d+)", item)
        if item.isdigit() or faixa:
            a, b = sorted((int(faixa.group(1)), int(faixa.group(2)))) if faixa else (int(item), int(item))
            if a < 1 or b > len(rows):
                problemas.append(f"Número/faixa {item} fora de /lista (1 a {len(rows)}).")
                continue
            for n in range(a, b + 1):
                x = rows[n - 1]
                remover[x["ref"]] = x
        else:
            exatos = [x for x in rows if normalizar(x["titulo"]) == normalizar(item)]
            matches = exatos or [x for x in rows if normalizar(item) in normalizar(x["titulo"])]
            if len(matches) == 1:
                remover[matches[0]["ref"]] = matches[0]
            elif not matches:
                problemas.append(f"{item}: não encontrado")
            else:
                problemas.append(f"{item}: mais de uma obra encontrada; use o número da /lista")
    for ref in remover:
        apagar_seguido(inter.user.id, ref)
    db.commit()
    linhas = (["🗑️ **Removidos:**"] + [f"• {x['titulo']} · {x['tipo']}" for x in remover.values()]) if remover else []
    if problemas:
        linhas.extend(["", "⚠️ **Não removidos:**", *[f"• {x}" for x in problemas]])
    if not linhas:
        linhas.append("Nada foi removido.")
    linhas.append("Os números mudam após remover. Veja `/lista` novamente.")
    await inter.response.send_message("\n".join(linhas)[:1900], ephemeral=True)


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
    def __init__(self, usuario: int, titulo: str, tipo: str, plataforma: str,
                 resultados: list, fontes: Optional[dict[int, str]] = None):
        super().__init__(timeout=180)
        self.usuario, self.titulo, self.tipo, self.plataforma = usuario, titulo, tipo, plataforma
        self.fontes = fontes or {}
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
            if self.fontes:
                gravar_links(inter.user.id, provider, mid, self.fontes)
            db.commit()
        except sqlite3.IntegrityError:
            return await inter.followup.send("Esse título já está na sua lista. Use `/meus_conteudos`.", ephemeral=True)
        self.stop()
        await inter.edit_original_response(content="✅ **Acompanhamento ativado!**\n"
                                           f"**{discord.utils.escape_markdown(titulo)}** · {self.tipo} · {self.plataforma}\n"
                                           "Escolha os canais em `/canal_conteudo` ou os destinatários em `/dm`.\n"
                                           "Filmes recebem aviso de **estreia**, não de episódios."
                                           + (f"\n🔗 {len(self.fontes)} link(s) de plataforma/temporada salvo(s)."
                                              if self.fontes else ""), view=None)


@bot.tree.command(name="seguir_conteudo", description="Seguir doramas, K-dramas, séries, filmes e animes (TMDB/TVmaze)")
@app_commands.describe(titulo="Nome da obra", tipo="Categoria", plataforma="Streaming onde você pretende assistir",
                       link="Opcional: link da obra ou temporada",
                       temporada="Opcional: temporada correspondente ao link (T1, T2...)",
                       temporadas="Opcional: T1: URL; T2: URL; T3: URL")
@app_commands.choices(tipo=TIPOS_CONTEUDO)
async def seguir_conteudo(inter: discord.Interaction, titulo: str, tipo: app_commands.Choice[str],
                          plataforma: str, link: Optional[str] = None,
                          temporada: Optional[app_commands.Range[int, 1, 100]] = None,
                          temporadas: Optional[str] = None):
    titulo, plataforma = titulo.strip(), plataforma.strip()
    if not titulo or not plataforma or len(titulo) > 150 or len(plataforma) > 100:
        return await inter.response.send_message("Informe título e plataforma válidos (até 150 e 100 caracteres).", ephemeral=True)
    try:
        fontes = ler_links_opcionais(link, temporada, temporadas)
    except ValueError as exc:
        return await inter.response.send_message(f"⚠️ {exc}", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    resultados = await resultados_por_categoria(titulo, tipo.value)
    if not resultados:
        detalhe = " Configure TMDB_API_KEY na FadeHost." if not TMDB_API_KEY else ""
        return await inter.followup.send("Não encontrei esse título nas APIs." + detalhe, ephemeral=True)
    await inter.followup.send("🔎 **Selecione o título correto** para acompanhar seus lançamentos:",
                              view=ResultadosConteudo(inter.user.id, titulo, tipo.value, plataforma, resultados[:25], fontes),
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
    row = db.execute("SELECT origem,mid FROM conteudos_seguidos WHERE id=?", (item_id,)).fetchone()
    db.execute("DELETE FROM conteudos_seguidos WHERE id=? AND user_id=?", (item_id, inter.user.id))
    if row and row[0] and row[1]:
        existe = db.execute("""SELECT 1 FROM conteudos_seguidos
                  WHERE user_id=? AND origem=? AND mid=? LIMIT 1""",
                  (inter.user.id, row[0], row[1])).fetchone()
        if not existe:
            db.execute("DELETE FROM links_temporadas WHERE user_id=? AND origem=? AND mid=?",
                       (inter.user.id, row[0], row[1]))
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
    tit = f"🎬 Estreia registrada: {titulo}" if filme else f"📺 Novo episódio: {titulo}"
    timestamp = int(ep["quando"].timestamp())
    # O Discord renderiza <t:...:R> como "há 4 dias", "há 2 horas" etc.,
    # e atualiza o texto relativo automaticamente. A data é da API de catálogo,
    # NÃO necessariamente da disponibilidade no serviço brasileiro.
    desc = (f"{'🎬 Estreia registrada' if filme else '🆕 Episódio lançado'} "
            f"<t:{timestamp}:R> (data: <t:{timestamp}:D>).\n"
            "⚠️ A data informada é a de estreia/exibição cadastrada na fonte e "
            "**não garante disponibilidade** na plataforma no Brasil.\n")
    if not filme:
        desc += f"Temporada **{ep['temporada']}**, episódio **{ep['numero']}**"
        if ep.get("nome"):
            desc += f" · {discord.utils.escape_markdown(ep['nome'])[:90]}"
        desc += "\n"
        if ep.get("total_lancados") is not None:
            desc += f"📊 **Total de episódios já lançados: {ep['total_lancados']}**\n"
    desc += f"🎞️ Categoria: **{tipo}**\n📺 Plataforma indicada: **{plataforma}**"
    url = (f"https://www.themoviedb.org/{'movie' if filme else 'tv'}/{mid}"
           if origem == "tmdb" else f"https://www.tvmaze.com/shows/{mid}")
    emb = discord.Embed(title=tit[:250], url=url, description=desc[:4000], color=0x5865F2)
    if poster_url and poster_url.startswith("https://"):
        emb.set_thumbnail(url=poster_url)
    emb.set_footer(text=f"Informações: {ep['source']} · Datas sujeitas a alterações")
    return emb


_cache_catalogo: dict[tuple[str, int, str], tuple[float, dict]] = {}


async def buscar_fontes_catalogo(origem: str, mid: int, tipo: str) -> dict:
    """TMDB/JustWatch (disponibilidade BR), IMDb/TVmaze; URLs são de catálogo."""
    chave = (origem, int(mid), tipo)
    cache = _cache_catalogo.get(chave)
    if cache and time.monotonic() - cache[0] < 21600:
        return cache[1]
    resp = {"onde_assistir": None, "imdb": None, "site_oficial": None, "provedores": []}
    try:
        if origem == "tmdb":
            categoria = "movie" if tipo in TIPOS_FILME else "tv"
            fontes, ids = await asyncio.gather(
                tmdb_json(f"/{categoria}/{mid}/watch/providers"),
                tmdb_json(f"/{categoria}/{mid}/external_ids"),
                return_exceptions=True)
            if isinstance(fontes, dict):
                br = (fontes.get("results") or {}).get("BR") or {}
                resp["onde_assistir"] = url_legal_externa(br.get("link"))
                resp["provedores"] = [p.get("provider_name") for p in (br.get("flatrate") or [])
                                        if isinstance(p, dict) and p.get("provider_name")]
            if isinstance(ids, dict):
                resp["imdb"] = ids.get("imdb_id")
        elif origem == "tvmaze":
            show = await api_json(f"{TVMAZE_API}/shows/{int(mid)}") or {}
            resp["imdb"] = (show.get("externals") or {}).get("imdb")
            resp["site_oficial"] = url_legal_externa(show.get("officialSite"))
    except Exception as exc:
        print("Metadados de streaming indisponíveis:", type(exc).__name__)
    _cache_catalogo[chave] = (time.monotonic(), resp)
    return resp


def botoes_streaming_conteudo(titulo: str, tipo: str, plataforma: str,
                              origem: str, mid: int,
                              fontes: Optional[dict] = None,
                              links_cadastrados: Optional[list[tuple[int, str]]] = None) -> discord.ui.View:
    """Nunca apresenta link de catálogo como se fosse reprodução do episódio."""
    view = discord.ui.View(timeout=None)
    fontes = fontes or {}
    eh_filme = tipo in TIPOS_FILME
    urls = set()

    def botao(rotulo: str, url: Optional[str]):
        url = url_legal_externa(url)
        if not url or url in urls or len(view.children) >= 5:
            return
        urls.add(url)
        view.add_item(discord.ui.Button(label=rotulo[:80], url=url))

    # Links indicados por membros são da obra/temporada e têm prioridade.
    botoes_links_usuario(view, links_cadastrados or [], urls)
    # TMDB indica provedores por região, mas NÃO retorna URL do episódio.
    if fontes.get("onde_assistir"):
        botao("Onde assistir (Brasil) · JustWatch", fontes["onde_assistir"])
    if origem == "tmdb":
        tp = "movie" if eh_filme else "tv"
        botao("Ver plataformas no TMDB",
              f"https://www.themoviedb.org/{tp}/{mid}/watch?locale=BR")
    else:
        botao("Detalhes no TVmaze", f"https://www.tvmaze.com/shows/{mid}")

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
    botao(f"Pesquisar na {nome or 'plataforma'}", url_plataforma)
    imdb = fontes.get("imdb")
    if isinstance(imdb, str) and re.fullmatch(r"tt\d{5,12}", imdb):
        botao("Consultar IMDb", f"https://www.imdb.com/title/{imdb}/")
    botao("Site oficial da obra", fontes.get("site_oficial"))
    botao("Buscar onde assistir", "https://www.google.com/search?q=" +
          quote_plus(f"{titulo} onde assistir legalmente Brasil"))
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
                                poster_url: Optional[str] = None,
                                usuario_id: Optional[int] = None) -> bool:
    """Envia apenas lançamentos que já ocorreram, não marca aviso real como entregue."""
    eps = await buscar_lancamentos(origem, mid, tvmaze_id, tipo)
    agora = datetime.now(timezone.utc)
    anteriores = [ep for ep in eps if ep.get("quando") and ep["quando"] <= agora]
    if not anteriores:
        return False
    ep = max(anteriores, key=lambda e: e["quando"])
    await canal.send(embed=aviso_teste_conteudo(titulo, tipo, plataforma, origem,
                      mid, ep, poster_url),
                     view=botoes_streaming_conteudo(titulo, tipo, plataforma, origem, mid,
                             await buscar_fontes_catalogo(origem, mid, tipo),
                             links_de_usuarios([usuario_id], origem, mid, ep.get("temporada"))
                             if usuario_id else []))
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
        fontes = await buscar_fontes_catalogo(origem, mid, tipo)
        for cid, pessoas in subs_canais.items():
            if registro_ja_entregue("canal", cid, origem, mid, ep["key"], tipo):
                continue
            c = bot.get_channel(cid)
            if not c:
                continue
            try:
                await c.send(" ".join(f"<@{u}>" for u in pessoas), embed=emb,
                             view=botoes_streaming_conteudo(titulo, tipo, plataforma, origem, mid, fontes,
                                links_de_usuarios(list(pessoas), origem, mid, ep.get("temporada"))),
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
                                  view=botoes_streaming_conteudo(titulo, tipo, plataforma, origem, mid, fontes,
                                        links_de_usuarios([a[0] for a in assinantes], origem,
                                                          mid, ep.get("temporada"))))
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
            total_carregado, total_lancados = False, None
            for ep in lancamentos:
                # Os alertas de filmes referem-se à estreia cadastrada no TMDB,
                # não necessariamente à data de entrada em Netflix/Prime etc.
                if ep["quando"] and desde <= ep["quando"] <= agora:
                    if not total_carregado and rows[0][2] not in TIPOS_FILME:
                        total_lancados = await total_episodios_exibidos(
                            origem, mid, maze, rows[0][2], lancamentos)
                        total_carregado = True
                    if total_lancados is not None:
                        ep = {**ep, "total_lancados": total_lancados}
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


@bot.tree.command(name="testar_conteudo", description="Testa o último episódio/estreia de /seguir_conteudo")
@app_commands.guild_only()
@app_commands.describe(numero="Número mostrado em /meus_conteudos", canal="Canal do teste (opcional)")
async def testar_conteudo(inter: discord.Interaction, numero: app_commands.Range[int, 1, 100000],
                          canal: Optional[discord.TextChannel] = None):
    rows = db.execute("SELECT id,titulo,tipo FROM conteudos_seguidos WHERE user_id=? ORDER BY id",
                      (inter.user.id,)).fetchall()
    if numero > len(rows):
        return await inter.response.send_message("Número inválido. Veja `/meus_conteudos`.", ephemeral=True)
    ident, titulo, tipo = rows[numero-1]
    destino = canal or inter.channel
    erro = await validar_canal_teste(inter, destino)
    if erro:
        return await inter.response.send_message("⚠️ " + erro, ephemeral=True)
    await inter.response.defer(ephemeral=True)
    try:
        aviso = await preparar_teste(inter.user.id, tipo, selecionado=f"c:{ident}")
        if aviso is None:
            return await inter.followup.send("Não encontrei episódio/estreia lançado para este título. "
                                             "Se for cadastro antigo, refaça em `/seguir_conteudo`.", ephemeral=True)
        await destino.send(content=f"<@{inter.user.id}>",
                           allowed_mentions=discord.AllowedMentions(users=True), **aviso)
    except Exception as e:
        print("Erro no teste de conteúdo:", type(e).__name__, e)
        return await inter.followup.send(f"⚠️ Não consegui enviar o teste ({type(e).__name__}).", ephemeral=True)
    await inter.followup.send(f"✅ Teste de **{tipo}** de **{discord.utils.escape_markdown(titulo)}** "
                              f"enviado em {destino.mention}, com os botões.", ephemeral=True)


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
    import pymupdf as fitz  # API moderna do PyMuPDF, sem FutureWarning de fitz
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


def url_legal_externa(url: Optional[str]) -> Optional[str]:
    """Só constrói botões para URLs HTTPS sem credenciais e sem hosts locais."""
    if not isinstance(url, str) or not 8 <= len(url) <= 1800:
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or not host or "." not in host
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return None
        if (host == "localhost" or host.endswith((".local", ".internal"))
                or host.startswith(("127.", "10.", "192.168.", "169.254.", "172.16."))):
            return None
        if any(ch.isspace() for ch in url):
            return None
        return url
    except ValueError:
        return None


def numero_episodio_em_titulo(titulo: str) -> Optional[int]:
    """Exige número explícito de episódio; não usa números aleatórios do título."""
    if not isinstance(titulo, str):
        return None
    texto = unicodedata.normalize("NFKD", titulo).encode("ascii", "ignore").decode().lower()
    padroes = (
        r"\bs\d{1,3}\s*e\s*0*(\d{1,4})\b",  # S01E03
        r"\b(?:episode|episodio|episodios|ep)\s*[#.:\-]?\s*0*(\d{1,4})\b",
        r"^\s*#?0*(\d{1,4})(?:\s*[-:|.]\s*|\s*$)",
    )
    for padrao in padroes:
        m = re.search(padrao, texto, re.I)
        if m:
            num = int(m.group(1))
            if 1 <= num <= 9999:
                return num
    return None


def encontrar_streaming_episodio(media: dict, episodio: int) -> Optional[dict]:
    """Só liga ao episódio exato nos dados 'streamingEpisodes' do AniList.

    Links gerais da obra / streamingLinks NÃO são URLs de episódio.
    """
    if not isinstance(episodio, int) or episodio < 1:
        return None
    encontrados = []
    for item in media.get("streamingEpisodes") or []:
        if numero_episodio_em_titulo(item.get("title") or "") != episodio:
            continue
        url = url_legal_externa(item.get("url"))
        if not url:
            continue
        site = str(item.get("site") or "streaming")[:60]
        preferencial = any(re.search(p, site.lower()) for p in PLATAFORMAS)
        encontrados.append((not preferencial, site.lower(), {"url": url, "site": site}))
    encontrados.sort(key=lambda x: (x[0], x[1]))
    return encontrados[0][2] if encontrados else None


_cache_anilist_episodios: dict[int, tuple[float, dict]] = {}


async def enriquecer_midia_anilist(media: dict) -> dict:
    """Consulta informações dos episódios apenas se ainda não vierem da busca."""
    if "streamingEpisodes" in media and "idMal" in media:
        return media
    mid = media.get("id")
    if not mid:
        return media
    cache = _cache_anilist_episodios.get(int(mid))
    if cache and time.monotonic() - cache[0] < 3600:
        return {**media, **cache[1]}
    q = """query($id:Int){Media(id:$id,type:ANIME){id idMal
        streamingEpisodes{title site url}
        externalLinks{site url type}}}"""
    try:
        extra = (await anilist(bot.session, q, {"id": int(mid)})).get("Media") or {}
        if extra:
            _cache_anilist_episodios[int(mid)] = (time.monotonic(), extra)
        return {**media, **extra}
    except Exception as e:
        print("Consulta de links específicos do AniList falhou:", type(e).__name__)
        return media


def fontes_catalogo_anime(media: dict) -> list[dict]:
    """Bases adicionais apenas quando existe URL/identificador real."""
    resultados = []
    mal_id = media.get("idMal")
    if isinstance(mal_id, int) and mal_id > 0:
        resultados.append({"nome": "MyAnimeList", "url": f"https://myanimelist.net/anime/{mal_id}"})
    nomes = (("LiveChart", "livechart"), ("Kitsu", "kitsu"),
             ("IMDb", "imdb"), ("TV Time", "tvtime"),
             ("MyAnimeList", "myanimelist"))
    for item in media.get("externalLinks") or []:
        origem = str(item.get("site") or "").lower().replace(" ", "")
        link = url_legal_externa(item.get("url"))
        if not link:
            continue
        for nome_base, trecho in nomes:
            if trecho in origem and not any(x["nome"] == nome_base for x in resultados):
                resultados.append({"nome": nome_base, "url": link})
                break
    return resultados


def links_de_streaming(media: dict) -> list[dict]:
    """Prefere as plataformas que você assina; se não houver, mostra as demais legais."""
    todos = [l for l in (media.get("externalLinks") or []) if l.get("type") == "STREAMING"]
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
        desc = (f"Episódio lançado <t:{ep['airingAt']}:R> "
                f"(data: <t:{ep['airingAt']}:D>). "
                "O horário é o de exibição informado pelo AniList; "
                "a disponibilidade no streaming pode variar.")
        cor = 0xE91E63

    emb = discord.Embed(title=titulo, url=m["siteUrl"], description=desc, color=cor)
    emb.set_thumbnail(url=m["coverImage"]["large"])
    if modo != "lembrete" and (ep.get("episode") or 0) > 0:
        emb.add_field(name="📊 Episódios lançados nesta temporada",
                      value=str(ep["episode"]), inline=False)
    return emb, links


def kw_aviso(emb: discord.Embed, links: list[dict], com_botoes: bool,
             episodio_oficial: Optional[dict] = None,
             fontes: Optional[list[dict]] = None,
             links_cadastrados: Optional[list[tuple[int, str]]] = None) -> dict:
    """Um botão específico só existe se AniList fornecer URL do episódio EXATO."""
    kwargs = {"embed": emb}
    if not com_botoes:
        return kwargs
    view = discord.ui.View(timeout=None)
    urls = set()

    def botao(rotulo: str, url: Optional[str]):
        url = url_legal_externa(url)
        if not url or url in urls or len(view.children) >= 5:
            return
        urls.add(url)
        view.add_item(discord.ui.Button(label=rotulo[:80], url=url))

    if episodio_oficial:
        botao("▶️ Assistir episódio mais recente", episodio_oficial.get("url"))
    botoes_links_usuario(view, links_cadastrados or [], urls)
    # Os links externalLinks são geralmente da OBRA, não de um episódio específico.
    for link in links[:3]:
        botao("Ver na " + str(link.get("site") or "plataforma"), link.get("url"))
        if len(view.children) >= 3:
            break
    # Bases extras com identificadores confirmados; não prometem reprodução.
    for fonte in (fontes or [])[:4]:
        if len(view.children) >= 4:
            break
        botao("Consultar " + fonte["nome"], fonte.get("url"))
    botao("Ver no AniList", emb.url)
    if not view.children:
        titulo = (emb.title or "Anime").replace("🧪 TESTE — ", "").replace("📺 ", "")
        titulo = titulo.rsplit(" — Episódio ", 1)[0]
        botao("Buscar onde assistir", "https://www.google.com/search?q=" +
              quote_plus(f"{titulo} onde assistir legalmente Brasil"))
    kwargs["view"] = view
    return kwargs

async def enviar(ep: dict, lembrete: bool):
    m = ep["media"]
    emb, links = montar_aviso(ep, "lembrete" if lembrete else "lancamento")
    botoes = not lembrete
    episodio_oficial, fontes = None, []
    if botoes:
        midia = await enriquecer_midia_anilist(m)
        episodio_oficial = encontrar_streaming_episodio(midia, int(ep.get("episode") or 0))
        fontes = fontes_catalogo_anime(midia)
        if episodio_oficial:
            emb.add_field(name="▶️ Link do episódio", value=(
                f"Encontrado em **{discord.utils.escape_markdown(episodio_oficial['site'])}**. "
                "Use o botão de reprodução abaixo."), inline=False)
        else:
            emb.add_field(name="▶️ Link do episódio", value=(
                "O AniList não informou um link direto para este episódio. "
                "Use os botões de consulta e plataformas."), inline=False)

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
                                 **kw_aviso(emb, links, botoes, episodio_oficial, fontes,
                                     links_de_usuarios(list(donos), "anilist", m["id"])) )
        except discord.HTTPException as e:
            print(f"Falha ao avisar no canal {cid}: {e}")

    for uid in dms:
        try:
            user = await bot.fetch_user(uid)
            assinantes = [r[0] for r in db.execute("""SELECT dono_id FROM dm_categorias
                             WHERE membro_id=? AND tipo='Anime'""", (uid,))]
            await user.send(**kw_aviso(emb, links, botoes, episodio_oficial, fontes,
                                     links_de_usuarios(assinantes, "anilist", m["id"])))
        except discord.HTTPException as e:
            print(f"Falha ao enviar DM para {uid}: {e}")


# ---------------------------------------------------------------------------
# /testar: botão para testar qualquer categoria num canal escolhido.
# O aviso de teste não marca um lançamento real como entregue.
# ---------------------------------------------------------------------------
# Apenas testes: não altera as inscrições ou os registros de alertas reais.
JANELA_TESTE_DIAS = 7  # Prioridade para testes, não uma restrição obrigatória


async def ultimo_episodio(media_id: int) -> Optional[dict]:
    """Último episódio já exibido de um anime específico que o usuário selecionou."""
    q = """query($id:Int,$now:Int){Page(perPage:1){
        airingSchedules(mediaId:$id,airingAt_lesser:$now,sort:TIME_DESC){
            episode airingAt
            media{id type isAdult title{romaji english} siteUrl coverImage{large}
                  externalLinks{site url type}}}}}"""
    data = await anilist(bot.session, q, {"id": media_id, "now": int(time.time())})
    return next((ep for ep in data["Page"]["airingSchedules"]
                 if ep.get("media") and not ep["media"].get("isAdult")), None)


async def anime_recente_global() -> Optional[dict]:
    """Localiza episódio já exibido de anime diretamente no AniList, sem precisar seguir."""
    agora = int(time.time())
    # Busca o último episódio conhecido; a janela de 7 dias não bloqueia o teste.
    q = """query($agora:Int,$inicio:Int){Page(perPage:30){
        airingSchedules(airingAt_lesser:$agora,airingAt_greater:$inicio,sort:TIME_DESC){
            episode airingAt
            media{id type isAdult title{romaji english} siteUrl coverImage{large}
                  externalLinks{site url type}}}}}"""
    erros = []
    for dias in (2, 7, 30, 90):
        try:
            resposta = await anilist(bot.session, q, {"agora": agora, "inicio": agora - dias * 86400})
            opcoes = [x for x in (resposta.get("Page") or {}).get("airingSchedules", [])
                      if x.get("media") and x["media"].get("type") == "ANIME"
                      and not x["media"].get("isAdult") and (x.get("episode") or 0) > 0
                      and x.get("airingAt") and x["airingAt"] <= agora]
            if opcoes:
                # Prefere links de streaming; mantém episódio real e já exibido.
                com_links = [x for x in opcoes if any(
                    l.get("type") == "STREAMING" and str(l.get("url", "")).startswith("https://")
                    for l in (x["media"].get("externalLinks") or []))]
                return max(com_links or opcoes, key=lambda x: x["airingAt"])
        except Exception as e:
            erros.append(f"{type(e).__name__}: {e}")
            break  # Erro de API não é ausência de episódio.
    if erros:
        print("AniList indisponível no teste global:", erros[0])
    return None


def episodio_tvmaze_valido(item: dict, categoria: str) -> Optional[dict]:
    """Filtra programação pública por categoria sem fingir que uma obra é outra."""
    show = item.get("show") or {}
    titulo = show.get("name")
    sid = show.get("id")
    if not titulo or not sid or not item.get("number"):
        return None
    tipo_show = str(show.get("type") or "").lower()
    generos = [str(g).lower() for g in show.get("genres") or []]
    if "animation" in tipo_show or "anime" in generos:
        return None
    if categoria == "Reality / programa" and not any(
        t in tipo_show for t in ("reality", "variety", "game show", "talk show")):
        return None
    if categoria == "Série" and any(
        t in tipo_show for t in ("reality", "variety", "game show", "talk show")):
        return None
    stamp = item.get("airstamp")
    try:
        quando = (datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)
                  if stamp else data_utc(item.get("airdate")))
    except (ValueError, TypeError):
        quando = data_utc(item.get("airdate"))
    if not quando or quando > datetime.now(timezone.utc):
        return None
    plataforma = ((show.get("webChannel") or {}).get("name") or
                  (show.get("network") or {}).get("name") or "Não informada")
    imagem = (show.get("image") or {}).get("medium")
    episodio = {"key": ident_ep(item.get("season") or 0, item["number"]),
                "quando": quando, "numero": item["number"],
                "temporada": item.get("season") or 0,
                "nome": item.get("name") or "", "source": "TVmaze"}
    return {"quando": quando, "titulo": titulo, "tipo": categoria,
            "plataforma": plataforma, "origem": "tvmaze", "mid": int(sid),
            "poster": imagem, "ep": episodio}


async def exemplo_publico_teste(categoria: str) -> Optional[dict]:
    """Exemplo público, usado apenas em /teste e /testedm, sem criar acompanhamentos."""
    hoje = datetime.now(FUSO_BR).date()
    if categoria in TIPOS_FILME:
        # TMDB cobre estreias de filmes; filmes não possuem episódios.
        parametros = {"sort_by": "primary_release_date.desc", "include_adult": "false",
                      "primary_release_date.lte": hoje.isoformat(),
                      "primary_release_date.gte": (hoje - timedelta(days=60)).isoformat(),
                      "page": 1}
        if categoria == "Filme anime":
            parametros.update({"with_genres": "16", "with_original_language": "ja"})
        dados = await tmdb_json("/discover/movie", parametros) or {}
        for filme in dados.get("results") or []:
            data = data_utc(filme.get("release_date"))
            if not data or data > datetime.now(timezone.utc) or not filme.get("id"):
                continue
            titulo = filme.get("title") or filme.get("original_title") or "Filme"
            poster = ("https://image.tmdb.org/t/p/w500" + filme["poster_path"]
                      if filme.get("poster_path") else None)
            return {"quando": data, "titulo": titulo, "tipo": categoria,
                    "plataforma": "A consultar", "origem": "tmdb", "mid": filme["id"],
                    "poster": poster, "ep": {"key": "ESTREIA", "quando": data,
                       "numero": None, "temporada": None, "nome": titulo, "source": "TMDB"}}
        return None

    # São fontes públicas reais: K-drama (KR), dorama (JP) e séries globais via web.
    # Pode não haver dados para todas as obras; nunca inventa episódios.
    if categoria == "K-drama":
        caminho, base = "/schedule", {"country": "KR"}
    elif categoria == "Dorama":
        caminho, base = "/schedule", {"country": "JP"}
    elif categoria in ("Série", "Reality / programa", "Outro"):
        caminho, base = "/schedule/web", {}
    else:
        return None
    melhor = None
    for dias in range(7):
        dia = (hoje - timedelta(days=dias)).isoformat()
        dados = await api_json(f"{TVMAZE_API}{caminho}", {**base, "date": dia}) or []
        for item in dados:
            candidato = episodio_tvmaze_valido(item, categoria)
            if candidato and (melhor is None or candidato["quando"] > melhor["quando"]):
                melhor = candidato
        if melhor:
            break  # Percorre a data mais recente primeiro; evita consultas desnecessárias.
        await asyncio.sleep(0.1)
    return melhor


# Itens já cadastrados em /seguir e /seguir_conteudo, sempre limitados
# ao membro que executa o teste. O identificador é interno, não o título.
def titulos_seguidos_teste(usuario_id: int, categoria: Optional[str] = None) -> list[dict]:
    itens = []
    if categoria is None or categoria == "Anime":
        for mid, titulo in db.execute(
            "SELECT media_id,title FROM follows WHERE user_id=? ORDER BY title COLLATE NOCASE",
            (usuario_id,)).fetchall():
            itens.append({"ref": f"f:{mid}", "titulo": titulo,
                          "tipo": "Anime", "via": "/seguir"})
    query = ("SELECT id,titulo,tipo FROM conteudos_seguidos WHERE user_id=? "
             + ("AND tipo=? " if categoria else "") + "ORDER BY titulo COLLATE NOCASE")
    params = (usuario_id, categoria) if categoria else (usuario_id,)
    for item_id, titulo, tipo in db.execute(query, params).fetchall():
        itens.append({"ref": f"c:{item_id}", "titulo": titulo,
                      "tipo": tipo, "via": "/seguir_conteudo"})
    return sorted(itens, key=lambda x: (x["titulo"].casefold(), x["via"]))


def titulo_seguido_ref(usuario_id: int, referencia: str) -> Optional[dict]:
    if not re.fullmatch(r"[fc]:[0-9]+", referencia or ""):
        return None
    prefixo, numero = referencia.split(":", 1)
    if prefixo == "f":
        row = db.execute("SELECT media_id,title FROM follows WHERE user_id=? AND media_id=?",
                         (usuario_id, int(numero))).fetchone()
        return ({"ref": referencia, "titulo": row[1], "tipo": "Anime", "via": "/seguir"}
                if row else None)
    row = db.execute("SELECT id,titulo,tipo FROM conteudos_seguidos WHERE user_id=? AND id=?",
                     (usuario_id, int(numero))).fetchone()
    return ({"ref": referencia, "titulo": row[1], "tipo": row[2], "via": "/seguir_conteudo"}
            if row else None)


async def titulos_teste_autocomplete(inter: discord.Interaction, atual: str):
    """Lista unificada: nomes do /seguir e do /seguir_conteudo."""
    cat = getattr(inter.namespace, "categoria", None)
    categoria = getattr(cat, "value", cat) if cat else None
    if categoria not in TIPOS_ALERTA:
        categoria = None
    itens = [x for x in titulos_seguidos_teste(inter.user.id, categoria)
             if atual.casefold() in x["titulo"].casefold()]
    return [app_commands.Choice(
        name=f"{x['titulo'][:71]} · {x['tipo']} ({x['via']})"[:100],
        value=x["ref"]) for x in itens[:25]]


async def conteudos_teste_autocomplete(inter: discord.Interaction, atual: str):
    """Sugestões do /seguir_conteudo que o membro realmente cadastrou."""
    cat = getattr(inter.namespace, "categoria", None)
    categoria = getattr(cat, "value", cat) if cat else None
    if categoria not in TIPOS_ALERTA:
        categoria = None
    itens = [x for x in titulos_seguidos_teste(inter.user.id, categoria)
             if x["ref"].startswith("c:")
             and atual.casefold() in x["titulo"].casefold()]
    return [app_commands.Choice(
        name=f"{x['titulo'][:76]} ({x['tipo']})"[:100], value=x["ref"][2:])
            for x in itens[:25]]


def obra_pertence_usuario(usuario_id: int, obra: str) -> Optional[tuple[str, int, str]]:
    item = titulo_seguido_ref(usuario_id, obra)
    if not item:
        return None
    if obra.startswith("f:"):
        return "anilist", int(obra[2:]), item["titulo"]
    if obra.startswith("c:"):
        row = db.execute("SELECT origem,mid,titulo FROM conteudos_seguidos WHERE id=? AND user_id=?",
                         (int(obra[2:]), usuario_id)).fetchone()
        if row and row[0] and row[1]:
            return row[0], int(row[1]), row[2]
    return None


@bot.tree.command(name="links_temporadas", description="Salvar links opcionais T1, T2, T3... de uma obra seguida")
@app_commands.describe(obra="Selecione uma obra de /seguir ou /seguir_conteudo",
                       links="Ex.: T1: https://site/1; T2: https://site/2; Geral: https://site/obra")
@app_commands.autocomplete(obra=titulos_teste_autocomplete)
async def links_temporadas(inter: discord.Interaction, obra: str, links: str):
    registro = obra_pertence_usuario(inter.user.id, obra)
    if not registro:
        return await inter.response.send_message("Escolha uma obra já cadastrada nas sugestões.", ephemeral=True)
    try:
        fontes = ler_links_opcionais(temporadas=links)
    except ValueError as exc:
        return await inter.response.send_message(f"⚠️ {exc}", ephemeral=True)
    if not fontes:
        return await inter.response.send_message("Adicione pelo menos um link.", ephemeral=True)
    origem, mid, titulo = registro
    gravar_links(inter.user.id, origem, mid, fontes)
    await inter.response.send_message(
        f"✅ **{discord.utils.escape_markdown(titulo)}**: {len(fontes)} link(s) salvos. "
        "As datas continuam sendo verificadas nas APIs; os links abrem a obra ou temporada.",
        ephemeral=True)


@bot.tree.command(name="link_temporada", description="Adicionar ou atualizar um link geral ou de uma temporada")
@app_commands.describe(obra="Selecione uma obra acompanhada", url="Link HTTPS da obra/temporada",
                       temporada="T1, T2, T3... (deixe vazio para link geral da obra)")
@app_commands.autocomplete(obra=titulos_teste_autocomplete)
async def link_temporada(inter: discord.Interaction, obra: str, url: str,
                         temporada: Optional[app_commands.Range[int, 1, 100]] = None):
    registro = obra_pertence_usuario(inter.user.id, obra)
    if not registro:
        return await inter.response.send_message("Escolha uma obra cadastrada nas sugestões.", ephemeral=True)
    try:
        link = normalizar_link_plataforma(url)
    except ValueError as exc:
        return await inter.response.send_message(f"⚠️ {exc}", ephemeral=True)
    origem, mid, titulo = registro
    gravar_links(inter.user.id, origem, mid, {temporada or 0: link})
    await inter.response.send_message(
        f"✅ Link de **{discord.utils.escape_markdown(titulo)}** "
        f"({'T'+str(temporada) if temporada else 'geral'}) atualizado:\n{link}", ephemeral=True)


@bot.tree.command(name="temporada_anime", description="Definir temporada correspondente ao anime seguido")
@app_commands.describe(obra="Escolha um anime da lista /seguir", temporada="Número atual da temporada (1 a 100)")
@app_commands.autocomplete(obra=titulos_teste_autocomplete)
async def temporada_anime(inter: discord.Interaction, obra: str,
                          temporada: app_commands.Range[int, 1, 100]):
    if not obra.startswith("f:") or not obra_pertence_usuario(inter.user.id, obra):
        return await inter.response.send_message("Selecione um anime cadastrado com `/seguir`.", ephemeral=True)
    mid = int(obra[2:])
    db.execute("INSERT OR REPLACE INTO anime_temporada_ativa VALUES(?,?,?)",
               (inter.user.id, mid, temporada))
    db.commit()
    await inter.response.send_message(f"✅ Anime associado à **T{temporada}** para escolher os links corretos.",
                                      ephemeral=True)


@bot.tree.command(name="ver_links", description="Mostrar os links cadastrados para cada temporada")
@app_commands.describe(obra="Obra seguida em /seguir ou /seguir_conteudo")
@app_commands.autocomplete(obra=titulos_teste_autocomplete)
async def ver_links(inter: discord.Interaction, obra: str):
    registro = obra_pertence_usuario(inter.user.id, obra)
    if not registro:
        return await inter.response.send_message("Escolha uma obra cadastrada nas sugestões.", ephemeral=True)
    origem, mid, titulo = registro
    rows = db.execute("""SELECT temporada,url FROM links_temporadas
                    WHERE user_id=? AND origem=? AND mid=? ORDER BY temporada""",
                    (inter.user.id, origem, mid)).fetchall()
    if not rows:
        return await inter.response.send_message("Esta obra não tem links opcionais cadastrados.", ephemeral=True)
    linhas = [f"**{'T'+str(n) if n else 'Geral'}:** <{url}>" for n, url in rows]
    await inter.response.send_message((f"🔗 **{discord.utils.escape_markdown(titulo)}**\n" +
                                      "\n".join(linhas))[:1900], ephemeral=True)


@bot.tree.command(name="remover_link", description="Remover link geral ou de uma temporada")
@app_commands.describe(obra="Obra seguida", temporada="Número da temporada, vazio = link geral")
@app_commands.autocomplete(obra=titulos_teste_autocomplete)
async def remover_link(inter: discord.Interaction, obra: str,
                       temporada: Optional[app_commands.Range[int, 1, 100]] = None):
    registro = obra_pertence_usuario(inter.user.id, obra)
    if not registro:
        return await inter.response.send_message("Escolha uma obra cadastrada nas sugestões.", ephemeral=True)
    origem, mid, titulo = registro
    cursor = db.execute("""DELETE FROM links_temporadas
                        WHERE user_id=? AND origem=? AND mid=? AND temporada=?""",
                        (inter.user.id, origem, mid, temporada or 0))
    db.commit()
    await inter.response.send_message(
        ("✅ Link removido." if cursor.rowcount else "Não havia link cadastrado nesta temporada."), ephemeral=True)


async def total_episodios_exibidos(origem: str, mid: int, maze: Optional[int],
                                   tipo: str, ja_coletados: Optional[list] = None) -> Optional[int]:
    """Contagem de episódios com data de exibição passada, excluindo especiais.

    Para títulos TMDB, consulta todas as temporadas: buscar_lancamentos costuma
    retornar apenas as últimas e não serve para contar a série inteira.
    """
    if tipo in TIPOS_FILME:
        return None
    agora = datetime.now(timezone.utc)
    try:
        if maze or origem == "tvmaze":
            eps = await coletar_tv_tvmaze(int(maze or mid))
        elif origem == "tmdb":
            dados = await tmdb_json(f"/tv/{mid}") or {}
            temporadas = [int(t["season_number"]) for t in dados.get("seasons") or []
                          if isinstance(t.get("season_number"), int) and t["season_number"] > 0]
            # Limita concorrência para respeitar as APIs e o tempo do Discord.
            sem = asyncio.Semaphore(4)
            async def uma_temporada(n):
                async with sem:
                    info = await tmdb_json(f"/tv/{mid}/season/{n}") or {}
                    return [dict(key=ident_ep(n, ep["episode_number"]), quando=data_utc(ep.get("air_date")))
                            for ep in info.get("episodes") or [] if ep.get("episode_number")]
            lotes = await asyncio.gather(*(uma_temporada(n) for n in temporadas))
            eps = [ep for lote in lotes for ep in lote]
        else:
            eps = ja_coletados or []
        return len({e["key"] for e in eps if e.get("quando") and e["quando"] <= agora
                    and not str(e.get("key", "")).startswith("S00")})
    except Exception as e:
        print("Não foi possível contar episódios exibidos:", type(e).__name__, e)
        return None


async def montar_teste_registro(registro: dict, episodios: Optional[list] = None) -> dict:
    ep = dict(registro["ep"])
    total = await total_episodios_exibidos(registro["origem"], registro["mid"],
                 registro.get("maze"), registro["tipo"], episodios)
    if total is not None:
        ep["total_lancados"] = total
    emb = aviso_teste_conteudo(registro["titulo"], registro["tipo"],
            registro["plataforma"], registro["origem"], registro["mid"],
            ep, registro.get("poster"))
    fontes = await buscar_fontes_catalogo(registro["origem"], registro["mid"], registro["tipo"])
    return {"embed": emb, "view": botoes_streaming_conteudo(registro["titulo"],
            registro["tipo"], registro["plataforma"], registro["origem"],
            registro["mid"], fontes,
            links_de_usuarios([registro["usuario"]], registro["origem"],
                              registro["mid"], ep.get("temporada"))
            if registro.get("usuario") else [])}


async def montar_teste_anime(ep: dict, usuario_id: Optional[int] = None) -> dict:
    emb, links = montar_aviso(ep, "teste")
    midia = await enriquecer_midia_anilist(ep["media"])
    episodio_oficial = encontrar_streaming_episodio(midia, int(ep.get("episode") or 0))
    if episodio_oficial:
        emb.add_field(name="▶️ Link do episódio", value=(
            f"Encontrado em **{discord.utils.escape_markdown(episodio_oficial['site'])}**. "
            "Use o botão de reprodução abaixo."), inline=False)
    else:
        emb.add_field(name="▶️ Link do episódio", value=(
            "Não foi encontrado link direto oficial deste episódio nas fontes consultadas. "
            "Os demais botões levam a páginas de catálogo ou de busca."), inline=False)
    return kw_aviso(emb, links, True, episodio_oficial, fontes_catalogo_anime(midia),
                    links_de_usuarios([usuario_id], "anilist", ep["media"]["id"])
                    if usuario_id else [])


async def preparar_teste(usuario_id: int, categoria: str, anime_id: Optional[int] = None,
                         selecionado: Optional[str] = None):
    """Testa título escolhido ou exemplo público; não marca aviso como entregue."""
    if selecionado:
        item = titulo_seguido_ref(usuario_id, selecionado)
        if not item or item["tipo"] != categoria:
            return None
        if selecionado.startswith("f:"):
            ep = await ultimo_episodio(int(selecionado[2:]))
            return await montar_teste_anime(ep, usuario_id) if ep else None
        reg = db.execute("""SELECT titulo,tipo,plataforma,origem,mid,tvmaze_id,poster_url
            FROM conteudos_seguidos WHERE user_id=? AND id=?""",
            (usuario_id, int(selecionado[2:]))).fetchone()
        if not reg or not reg[3] or not reg[4]:
            return None  # Registros antigos sem identificador precisam ser recadastrados.
        titulo, tipo, plataforma, origem, mid, maze, poster = reg
        episodios = await buscar_lancamentos(origem, mid, maze, tipo)
        agora = datetime.now(timezone.utc)
        existentes = [e for e in episodios if e.get("quando") and e["quando"] <= agora]
        if not existentes:
            return None
        ep = max(existentes, key=lambda e: e["quando"])
        return await montar_teste_registro({"titulo": titulo, "tipo": tipo,
            "plataforma": plataforma, "origem": origem, "mid": mid,
            "maze": maze, "poster": poster, "ep": ep, "usuario": usuario_id}, episodios)

    if categoria == "Anime":
        if anime_id is not None:
            return await preparar_teste(usuario_id, categoria, selecionado=f"f:{anime_id}")
        try:
            ep = await anime_recente_global()
            if ep:
                return await montar_teste_anime(ep, usuario_id)
        except Exception as e:
            print("Falha ao buscar anime recente global:", type(e).__name__, e)
        melhor = None
        ids = [r[0] for r in db.execute("SELECT media_id FROM follows WHERE user_id=?", (usuario_id,))]
        for mid in ids:
            try:
                ep = await ultimo_episodio(mid)
                if ep and (melhor is None or ep["airingAt"] > melhor["airingAt"]):
                    melhor = ep
            except Exception as e:
                print("Falha ao consultar anime seguido para teste:", type(e).__name__, e)
        return await montar_teste_anime(melhor, usuario_id) if melhor else None

    melhor = None
    rows = db.execute("""SELECT titulo,tipo,plataforma,origem,mid,tvmaze_id,poster_url
        FROM conteudos_seguidos WHERE user_id=? AND tipo=?
            AND origem IS NOT NULL AND mid IS NOT NULL""", (usuario_id, categoria)).fetchall()
    agora = datetime.now(timezone.utc)
    for titulo, tipo, plataforma, origem, mid, maze, poster in rows:
        try:
            episodios = await buscar_lancamentos(origem, mid, maze, tipo)
            antigos = [e for e in episodios if e.get("quando") and e["quando"] <= agora]
            if antigos:
                ep = max(antigos, key=lambda e: e["quando"])
                registro = {"quando": ep["quando"], "titulo": titulo, "tipo": tipo,
                            "plataforma": plataforma, "origem": origem, "mid": mid,
                            "maze": maze, "poster": poster, "ep": ep, "usuario": usuario_id}
                if melhor is None or ep["quando"] > melhor["quando"]:
                    melhor = registro
        except Exception as e:
            print("Falha ao consultar conteúdo seguido para teste:", type(e).__name__, e)
    if melhor and melhor["quando"] >= agora - timedelta(days=JANELA_TESTE_DIAS):
        return await montar_teste_registro(melhor)
    try:
        publico = await exemplo_publico_teste(categoria)
        if publico and (melhor is None or publico["quando"] > melhor["quando"]):
            melhor = publico
    except Exception as e:
        print("Consulta pública de teste falhou:", type(e).__name__, e)
    return await montar_teste_registro(melhor) if melhor else None


class TesteView(discord.ui.View):
    def __init__(self, usuario_id: int, categoria: str, canal_id: int,
                 media_id: Optional[int] = None, selecionado: Optional[str] = None):
        super().__init__(timeout=300)
        self.usuario_id, self.categoria = usuario_id, categoria
        self.canal_id, self.media_id, self.selecionado = canal_id, media_id, selecionado
        self.enviado = False

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.usuario_id:
            await inter.response.send_message("Somente quem iniciou este teste pode usar os botões.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Enviar aviso de teste", emoji="🧪", style=discord.ButtonStyle.primary)
    async def enviar_teste(self, inter: discord.Interaction, button: discord.ui.Button):
        if self.enviado:
            return await inter.response.send_message("Teste já enviado. Use `/teste` para repetir.", ephemeral=True)
        destino = inter.guild.get_channel(self.canal_id) if inter.guild else None
        if not destino:
            return await inter.response.send_message("O canal de teste não está disponível.", ephemeral=True)
        erro = await validar_canal_teste(inter, destino)
        if erro:
            return await inter.response.send_message("⚠️ " + erro, ephemeral=True)
        self.enviado = True
        await inter.response.defer(ephemeral=True)
        try:
            aviso = await preparar_teste(self.usuario_id, self.categoria,
                                          self.media_id, self.selecionado)
            if not aviso:
                self.enviado = False
                return await inter.followup.send(
                    "Não achei um episódio com data registrada para esta categoria. "
                    "Tente seguir um título com `/seguir_conteudo` ou verifique os logs das APIs na FadeHost.", ephemeral=True)
            await destino.send(content=f"<@{self.usuario_id}>",
                               allowed_mentions=discord.AllowedMentions(users=True), **aviso)
            button.disabled = True
            await inter.edit_original_response(view=self)
            await inter.followup.send(f"✅ Teste de **{self.categoria}** enviado em {destino.mention}.", ephemeral=True)
        except Exception as e:
            self.enviado = False
            print("Erro no teste de canal:", type(e).__name__, e)
            await inter.followup.send(f"⚠️ Não consegui enviar o teste: {type(e).__name__}.", ephemeral=True)


class SeletorCategoriaTeste(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="1. Escolha a categoria", min_values=1, max_values=1,
             options=[discord.SelectOption(label=t, value=t) for t in TIPOS_ALERTA], row=0)

    async def callback(self, inter: discord.Interaction):
        painel = self.view
        painel.categoria = self.values[0]
        painel.selecionado = None
        painel.pagina = 0
        painel.atualizar_titulos()
        await inter.response.edit_message(content=painel.descricao(), view=painel)


class SeletorCanalTeste(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="2. Escolha o canal (opcional)", min_values=1, max_values=1,
                         channel_types=[discord.ChannelType.text, discord.ChannelType.news], row=1)

    async def callback(self, inter: discord.Interaction):
        painel = self.view
        painel.canal_id = int(self.values[0].id)
        await inter.response.edit_message(content=painel.descricao(), view=painel)


class SeletorTituloTeste(discord.ui.Select):
    def __init__(self, itens: list[dict], pagina: int, selecionado: Optional[str]):
        total_paginas = max(1, (len(itens) + 23) // 24)
        atual = itens[pagina * 24:pagina * 24 + 24]
        opcoes = [discord.SelectOption(label="Episódio/estreia automático (exemplo)",
                                       value="auto", default=not selecionado)]
        for item in atual:
            opcoes.append(discord.SelectOption(
                label=item["titulo"][:100], value=item["ref"],
                description=f"{item['tipo']} · {item['via']}"[:100],
                default=item["ref"] == selecionado))
        super().__init__(placeholder=f"3. Escolha a obra cadastrada (página {pagina+1}/{total_paginas})",
                         options=opcoes, min_values=1, max_values=1, row=2)

    async def callback(self, inter: discord.Interaction):
        painel = self.view
        painel.selecionado = None if self.values[0] == "auto" else self.values[0]
        await inter.response.edit_message(content=painel.descricao(), view=painel)


class PainelTeste(discord.ui.View):
    def __init__(self, usuario_id: int, canal_id: int):
        super().__init__(timeout=300)
        self.usuario_id, self.canal_id = usuario_id, canal_id
        self.categoria = "Anime"
        self.selecionado = None
        self.pagina = 0
        self.add_item(SeletorCategoriaTeste())
        self.add_item(SeletorCanalTeste())
        self.atualizar_titulos()

    def atualizar_titulos(self):
        for child in list(self.children):
            if isinstance(child, SeletorTituloTeste):
                self.remove_item(child)
        itens = titulos_seguidos_teste(self.usuario_id, self.categoria)
        paginas = max(1, (len(itens) + 23) // 24)
        self.pagina = max(0, min(self.pagina, paginas - 1))
        self.add_item(SeletorTituloTeste(itens, self.pagina, self.selecionado))
        self.anterior.disabled = self.pagina == 0
        self.proxima.disabled = self.pagina >= paginas - 1

    def descricao(self):
        titulo = titulo_seguido_ref(self.usuario_id, self.selecionado) if self.selecionado else None
        escolhido = f"**{discord.utils.escape_markdown(titulo['titulo'])}**" if titulo else "Exemplo automático"
        return (f"🧪 **Teste de avisos**\nCategoria: **{self.categoria}**\n"
                f"Canal: <#{self.canal_id}>\nObra: {escolhido}\n\n"
                "Escolha uma obra que você cadastrou com `/seguir` ou `/seguir_conteudo`. "
                "Se deixar em automático, o bot procura um lançamento de exemplo. "
                "O teste mostra o episódio mais recente da obra escolhida, mesmo que seja antigo; "
                "não cria avisos reais.")

    async def interaction_check(self, inter: discord.Interaction):
        if inter.user.id != self.usuario_id:
            await inter.response.send_message("Somente quem abriu este painel pode usá-lo.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary, row=3)
    async def anterior(self, inter: discord.Interaction, button: discord.ui.Button):
        self.pagina = max(0, self.pagina - 1)
        self.atualizar_titulos()
        await inter.response.edit_message(content=self.descricao(), view=self)

    @discord.ui.button(label="Próxima", emoji="▶️", style=discord.ButtonStyle.secondary, row=3)
    async def proxima(self, inter: discord.Interaction, button: discord.ui.Button):
        self.pagina += 1
        self.atualizar_titulos()
        await inter.response.edit_message(content=self.descricao(), view=self)

    @discord.ui.button(label="Enviar aviso de teste", emoji="🧪", style=discord.ButtonStyle.primary, row=4)
    async def preparar(self, inter: discord.Interaction, button: discord.ui.Button):
        destino = inter.guild.get_channel(self.canal_id) if inter.guild else None
        if not destino:
            return await inter.response.send_message("Canal indisponível; selecione outro.", ephemeral=True)
        erro = await validar_canal_teste(inter, destino)
        if erro:
            return await inter.response.send_message("⚠️ " + erro, ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            aviso = await preparar_teste(self.usuario_id, self.categoria,
                                         selecionado=self.selecionado)
            if not aviso:
                return await inter.followup.send(
                    "Não encontrei um episódio/estreia com data para essa obra. "
                    "Se for um cadastro antigo, refaça em `/seguir_conteudo`.", ephemeral=True)
            await destino.send(content=f"<@{self.usuario_id}>",
                allowed_mentions=discord.AllowedMentions(users=True), **aviso)
            await inter.followup.send(f"✅ Teste de **{self.categoria}** enviado em {destino.mention}, "
                                      "com imagem, tempo relativo e botões.", ephemeral=True)
        except Exception as e:
            print("Falha no teste de canal:", type(e).__name__, e)
            await inter.followup.send(f"⚠️ Falha ao enviar teste ({type(e).__name__}).", ephemeral=True)


@bot.tree.command(name="teste", description="Escolher categoria, título seguido e canal para testar o aviso")
@app_commands.guild_only()
@app_commands.describe(canal="Canal inicial do teste (pode mudar no painel)")
async def teste(inter: discord.Interaction, canal: Optional[discord.TextChannel] = None):
    destino = canal or inter.channel
    painel = PainelTeste(inter.user.id, destino.id)
    await inter.response.send_message(painel.descricao(), view=painel, ephemeral=True)


@bot.tree.command(name="testar", description="Testar títulos de /seguir e /seguir_conteudo com botões")
@app_commands.guild_only()
@app_commands.describe(titulo="Selecione um título de /seguir ou /seguir_conteudo",
                       categoria="Categoria (opcional, filtra os títulos)",
                       canal="Canal que receberá o teste (padrão: canal atual)",
                       anime="Campo antigo: anime de /seguir (opcional)",
                       conteudo="Campo antigo: conteúdo de /seguir_conteudo (opcional)")
@app_commands.choices(categoria=TIPOS_CONTEUDO)
@app_commands.autocomplete(titulo=titulos_teste_autocomplete,
                            anime=seguindo_ac, conteudo=conteudos_teste_autocomplete)
async def testar(inter: discord.Interaction, titulo: Optional[str] = None,
                 categoria: Optional[app_commands.Choice[str]] = None,
                 canal: Optional[discord.TextChannel] = None,
                 anime: Optional[str] = None, conteudo: Optional[str] = None):
    destino = canal or inter.channel
    if sum(bool(item) for item in (titulo, anime, conteudo)) > 1:
        return await inter.response.send_message("Escolha apenas um título.", ephemeral=True)
    if not titulo and not anime and not conteudo and categoria is None:
        painel = PainelTeste(inter.user.id, destino.id)
        return await inter.response.send_message(painel.descricao(), view=painel, ephemeral=True)
    tipo = categoria.value if categoria else "Anime"
    selecionado = None
    if titulo:
        selecionado = titulo
    elif anime:
        if not anime.isdigit():
            return await inter.response.send_message("Escolha um anime das sugestões de /seguir.", ephemeral=True)
        selecionado = f"f:{anime}"
    elif conteudo:
        if not conteudo.isdigit():
            return await inter.response.send_message("Escolha um título das sugestões de /seguir_conteudo.", ephemeral=True)
        selecionado = f"c:{conteudo}"
    if selecionado:
        item = titulo_seguido_ref(inter.user.id, selecionado)
        if not item:
            return await inter.response.send_message("Título não está nos seus acompanhamentos.", ephemeral=True)
        if categoria and categoria.value != item["tipo"]:
            return await inter.response.send_message(
                f"O título selecionado é **{item['tipo']}**. Escolha esta categoria ou deixe em branco.", ephemeral=True)
        tipo = item["tipo"]
    erro = await validar_canal_teste(inter, destino)
    if erro:
        return await inter.response.send_message("⚠️ " + erro, ephemeral=True)
    escolhido = titulo_seguido_ref(inter.user.id, selecionado) if selecionado else None
    texto = (f"🧪 Teste **{tipo}** de **{discord.utils.escape_markdown(escolhido['titulo'])}**"
             if escolhido else f"🧪 Teste automático de **{tipo}**")
    await inter.response.send_message(
        f"{texto} → {destino.mention}. Clique para enviar.",
        view=TesteView(inter.user.id, tipo, destino.id, selecionado=selecionado), ephemeral=True)


# Testes DM são independentes dos avisos reais. Não alteram dm_categorias nem alertas_entregues.
# Repetições são mantidas na memória e param se o bot reiniciar.
tarefas_testedm: dict[int, asyncio.Task] = {}


async def destinatarios_validos_testedm(guild: discord.Guild, ids: list[int],
                                      usuario_id: int, categoria: str, repetir: bool):
    validos, erros = [], []
    for uid in ids:
        if db.execute("SELECT 1 FROM dm_bloqueios WHERE user_id=?", (uid,)).fetchone():
            erros.append(f"<@{uid}> bloqueou avisos por DM")
            continue
        try:
            membro = guild.get_member(uid) or await guild.fetch_member(uid)
        except discord.HTTPException:
            membro = None
        if membro is None or membro.bot:
            erros.append(f"<@{uid}> não é um membro humano deste servidor")
            continue
        if repetir and uid != usuario_id and not db.execute("""SELECT 1 FROM dm_categorias
                WHERE dono_id=? AND membro_id=? AND tipo=?""",
                (usuario_id, uid, categoria)).fetchone():
            erros.append(f"<@{uid}>: configure primeiro avisos dessa categoria com `/dm` para repetir")
            continue
        validos.append(membro)
    return validos, erros


async def enviar_testedm(guild: discord.Guild, usuario_id: int, ids: list[int],
                        categoria: str, repetir: bool = False,
                        selecionado: Optional[str] = None):
    membros, erros = await destinatarios_validos_testedm(guild, ids, usuario_id, categoria, repetir)
    if not membros:
        return 0, erros or ["Nenhum destinatário válido"]
    aviso = await preparar_teste(usuario_id, categoria, selecionado=selecionado)
    if aviso is None:
        return 0, [f"Nenhum episódio/estreia com data disponível em **{categoria}**. Tente `/seguir_conteudo`."]
    sucesso = 0
    for membro in membros:
        # Respeita bloqueio mesmo que alterado enquanto o envio estava em andamento.
        if db.execute("SELECT 1 FROM dm_bloqueios WHERE user_id=?", (membro.id,)).fetchone():
            continue
        try:
            await membro.send(**aviso)
            sucesso += 1
        except discord.HTTPException:
            erros.append(f"<@{membro.id}>: DM fechada ou envio recusado pelo Discord")
        await asyncio.sleep(0.35)
    return sucesso, erros


async def repetir_testedm(guild: discord.Guild, usuario_id: int, ids: list[int],
                           categoria: str, selecionado: Optional[str] = None):
    """Um envio imediato, depois a cada 30 minutos até /pausartestedm ou reinício."""
    try:
        while True:
            try:
                quantidade, erros = await enviar_testedm(guild, usuario_id, ids, categoria,
                                                          repetir=True, selecionado=selecionado)
                print(f"Teste DM repetido por {usuario_id}: {categoria}, enviados={quantidade}, falhas={len(erros)}")
                if not quantidade:
                    break  # Evita repetir indefinidamente se nenhum DM é possível.
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print("Erro no testedm automático:", type(e).__name__, e)
            await asyncio.sleep(1800)
    finally:
        if tarefas_testedm.get(usuario_id) is asyncio.current_task():
            tarefas_testedm.pop(usuario_id, None)


def pausar_testedm(usuario_id: int) -> bool:
    tarefa = tarefas_testedm.pop(usuario_id, None)
    if tarefa and not tarefa.done():
        tarefa.cancel()
        return True
    return False


class SeletorUsuariosTesteDm(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="1. Selecione até 25 @usuários", min_values=1, max_values=25, row=0)

    async def callback(self, inter: discord.Interaction):
        painel = self.view
        painel.ids = list(dict.fromkeys(int(u.id) for u in self.values))
        await inter.response.edit_message(content=painel.descricao(), view=painel)


class SeletorCategoriaTesteDm(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="2. Selecione a categoria", min_values=1, max_values=1,
            options=[discord.SelectOption(label=t, value=t) for t in TIPOS_ALERTA], row=1)

    async def callback(self, inter: discord.Interaction):
        painel = self.view
        painel.categoria = self.values[0]
        painel.selecionado = None
        painel.pagina = 0
        painel.atualizar_titulos()
        await inter.response.edit_message(content=painel.descricao(), view=painel)


class PainelTesteDm(discord.ui.View):
    def __init__(self, usuario_id: int, guild: discord.Guild, ids: Optional[list[int]] = None):
        super().__init__(timeout=600)
        self.usuario_id, self.guild = usuario_id, guild
        self.ids = ids or []
        self.categoria = "Anime"
        self.selecionado = None
        self.pagina = 0
        self.ultimo_envio = 0.0
        self.add_item(SeletorUsuariosTesteDm())
        self.add_item(SeletorCategoriaTesteDm())
        self.atualizar_titulos()

    def atualizar_titulos(self):
        for child in list(self.children):
            if isinstance(child, SeletorTituloTeste):
                self.remove_item(child)
        itens = titulos_seguidos_teste(self.usuario_id, self.categoria)
        paginas = max(1, (len(itens) + 23) // 24)
        self.pagina = max(0, min(self.pagina, paginas - 1))
        self.add_item(SeletorTituloTeste(itens, self.pagina, self.selecionado))
        self.anterior.disabled = self.pagina == 0
        self.proxima.disabled = self.pagina >= paginas - 1

    def descricao(self):
        usuarios = ", ".join(f"<@{u}>" for u in self.ids) or "nenhum selecionado"
        if len(usuarios) > 900:
            usuarios = usuarios[:900] + "…"
        status = "🟢 ativo" if self.usuario_id in tarefas_testedm and not tarefas_testedm[self.usuario_id].done() else "⏸️ parado"
        obra = titulo_seguido_ref(self.usuario_id, self.selecionado) if self.selecionado else None
        nome_obra = discord.utils.escape_markdown(obra["titulo"]) if obra else "Exemplo automático"
        return (f"📩 **Teste de avisos por DM**\n**Destinatários:** {usuarios}\n"
                f"**Categoria:** {self.categoria}\n**Obra:** {nome_obra}\n"
                f"**Repetição:** {status}\n\n"
                "**Enviar teste agora:** uma mensagem com episódio/estreia e botões para cada usuário.\n"
                "**Repetir a cada 30 min:** envia agora e continua até pausar. "
                "Para repetir em outros usuários, configure antes o `/dm` dessa categoria.\n"
                "**Pausar:** interrompe próximos testes. O destinatário pode usar `/sair_dos_avisos`.")

    async def interaction_check(self, inter: discord.Interaction):
        if inter.user.id != self.usuario_id:
            await inter.response.send_message("Somente quem abriu o painel pode usar estes botões.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary, row=3)
    async def anterior(self, inter: discord.Interaction, button: discord.ui.Button):
        self.pagina = max(0, self.pagina - 1)
        self.atualizar_titulos()
        await inter.response.edit_message(content=self.descricao(), view=self)

    @discord.ui.button(label="Próxima", emoji="▶️", style=discord.ButtonStyle.secondary, row=3)
    async def proxima(self, inter: discord.Interaction, button: discord.ui.Button):
        self.pagina += 1
        self.atualizar_titulos()
        await inter.response.edit_message(content=self.descricao(), view=self)

    @discord.ui.button(label="Enviar teste agora", emoji="📨", style=discord.ButtonStyle.primary, row=4)
    async def enviar_agora(self, inter: discord.Interaction, button: discord.ui.Button):
        if not self.ids:
            return await inter.response.send_message("Selecione um ou vários usuários primeiro.", ephemeral=True)
        if time.monotonic() - self.ultimo_envio < 30:
            return await inter.response.send_message("Espere 30 segundos para enviar outra rodada.", ephemeral=True)
        self.ultimo_envio = time.monotonic()
        await inter.response.defer(ephemeral=True)
        try:
            n, erros = await enviar_testedm(self.guild, self.usuario_id, self.ids,
                                              self.categoria, selecionado=self.selecionado)
            texto = f"📨 Teste de **{self.categoria}** enviado por DM para **{n}** membro(s)."
            if erros:
                texto += "\n⚠️ " + "\n⚠️ ".join(erros)
            await inter.followup.send(texto[:1900], ephemeral=True)
        except Exception as e:
            print("Falha no teste DM:", type(e).__name__, e)
            await inter.followup.send(f"⚠️ Falha ao testar DM ({type(e).__name__}).", ephemeral=True)

    @discord.ui.button(label="Repetir a cada 30 min", emoji="🔁", style=discord.ButtonStyle.secondary, row=4)
    async def iniciar_repeticao(self, inter: discord.Interaction, button: discord.ui.Button):
        if not self.ids:
            return await inter.response.send_message("Selecione um ou vários usuários primeiro.", ephemeral=True)
        if self.usuario_id in tarefas_testedm and not tarefas_testedm[self.usuario_id].done():
            return await inter.response.send_message("Já existe um teste repetido ativo. Use **Pausar** primeiro.", ephemeral=True)
        # Exige configuração prévia de /dm para todos os usuários de testes recorrentes.
        _, erros = await destinatarios_validos_testedm(self.guild, self.ids,
                            self.usuario_id, self.categoria, repetir=True)
        if erros:
            return await inter.response.send_message("⚠️ Corrija os destinatários antes de repetir:\n"+
                                                      "\n".join(erros)[:1600], ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            aviso = await preparar_teste(self.usuario_id, self.categoria,
                                          selecionado=self.selecionado)
        except Exception as e:
            print("Falha ao preparar a repetição por DM:", type(e).__name__, e)
            return await inter.followup.send("⚠️ Não foi possível consultar um episódio para o teste.", ephemeral=True)
        if aviso is None:
            return await inter.followup.send("Não encontrei episódio recente em **"
                + self.categoria + "**. Para séries/doramas, use `/seguir_conteudo`.", ephemeral=True)
        tarefa = asyncio.create_task(repetir_testedm(self.guild, self.usuario_id, self.ids[:],
                                                     self.categoria, self.selecionado))
        tarefas_testedm[self.usuario_id] = tarefa
        await inter.edit_original_response(content=self.descricao(), view=self)
        await inter.followup.send("🔁 Repetição iniciada, com primeiro envio agora. "
            "Próximas rodadas: a cada 30 minutos. Para parar, clique **Pausar testes** "
            "ou use `/pausartestedm`.", ephemeral=True)

    @discord.ui.button(label="Pausar testes", emoji="⏸️", style=discord.ButtonStyle.danger, row=4)
    async def pausar(self, inter: discord.Interaction, button: discord.ui.Button):
        pausou = pausar_testedm(self.usuario_id)
        await inter.response.edit_message(content=self.descricao(), view=self)
        await inter.followup.send("⏸️ Testes repetidos pausados." if pausou else
                                  "Não há repetição ativa. Testes únicos já enviados não podem ser desfeitos.",
                                  ephemeral=True)


@bot.tree.command(name="testedm", description="Testar DMs para vários usuários, escolher categoria e pausar")
@app_commands.guild_only()
@app_commands.describe(membros="Opcional: várias menções @usuário/IDs separadas por espaço; vazio abre seletor")
async def testedm(inter: discord.Interaction, membros: Optional[str] = None):
    ids = extrair_ids(membros) if membros else []
    if membros and (not ids or len(ids) > 25):
        return await inter.response.send_message("Informe entre 1 e 25 menções ou IDs válidos.", ephemeral=True)
    painel = PainelTesteDm(inter.user.id, inter.guild, ids)
    await inter.response.send_message(painel.descricao(), view=painel, ephemeral=True)


@bot.tree.command(name="pausartestedm", description="Para as repetições do /testedm que você iniciou")
async def pausartestedm(inter: discord.Interaction):
    pausou = pausar_testedm(inter.user.id)
    await inter.response.send_message("⏸️ Repetições do seu teste por DM pausadas." if pausou
        else "Você não tem testes por DM em repetição agora.", ephemeral=True)


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
