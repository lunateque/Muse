import anyio
import httpx
from dishka import Provider, Scope, provide

from muse.activity.domain import LikeRepository, PlayRepository
from muse.activity.infra.sql import SqlLikes, SqlPlays
from muse.activity.service import Likes, Plays
from muse.artwork.domain import ArtworkFiles, ArtworkLibrary, ArtworkStores
from muse.artwork.infra.files import LocalArtworkFiles
from muse.artwork.infra.sql import SqlArtworkLibrary
from muse.artwork.infra.stores import OnlineStores
from muse.artwork.service import Artwork
from muse.catalog.domain import (
    CatalogQueries,
    LibraryRoot,
    LibraryState,
    PlaylistSource,
    TrackStorage,
)
from muse.catalog.infra.files import TrackFiles
from muse.catalog.infra.library import LibraryFiles
from muse.catalog.infra.playlists import PlaylistFiles
from muse.catalog.infra.sql import SqlCatalog
from muse.catalog.service import Catalog
from muse.daily.domain import DailyStore, TasteSource
from muse.daily.infra.sql import SqlDailyStore, SqlTasteSource
from muse.daily.service import DailyMaker, DailyPlaylists
from muse.diagnostics.domain import PlayerLog
from muse.diagnostics.infra.player_log import PlayerLogFile
from muse.diagnostics.service import PlayerLogService
from muse.discovery.domain import GenreTracks, ListeningHistory, TextEncoder, VectorSource
from muse.discovery.infra.sql import SqlGenreTracks, SqlListeningHistory, SqlVectors
from muse.discovery.infra.text import HttpTextEncoder
from muse.discovery.service import (
    Mixes,
    MoodMemo,
    Moods,
    Radio,
    RadioModelCache,
    RadioModels,
    VectorIndexCache,
    VectorIndexes,
)
from muse.hls.domain import HlsStore
from muse.hls.infra.sql import SqlHls
from muse.hls.service import HlsSessions, SongCache
from muse.home.domain import HomeQueries
from muse.home.infra.sql import SqlHome
from muse.home.service import ContinueCards, HomeFeed, HomeRefresh, Shelves
from muse.identity.domain import SessionStore
from muse.identity.infra.sql import SqlSessions
from muse.identity.service import LoginService, SessionService
from muse.insights.domain import PlayStats
from muse.insights.infra.sql import SqlStats
from muse.insights.service import ListeningStats
from muse.lyrics.domain import LyricsSource
from muse.lyrics.infra.lrclib import Lrclib
from muse.lyrics.service import Lyrics
from muse.notifications.domain import SubscriptionRepository
from muse.notifications.infra.sql import SqlSubscriptions
from muse.notifications.service import Notifier, PushReactions, PushSubscriptions
from muse.pet.domain import PetRepository, QuestRepository
from muse.pet.infra.quests import SqlQuests
from muse.pet.infra.sql import SqlPets
from muse.pet.service import (
    HungerWatch,
    PetCare,
    PetKeeper,
    PetPushes,
    PetReactions,
    PetViews,
    QuestAlbums,
    Quests,
)
from muse.settings import Settings
from muse.together.dates import DatesSource
from muse.together.domain import (
    LetterBox,
    NotesSource,
    OursList,
    PinBoard,
    SecretBox,
    TogetherLedger,
)
from muse.together.infra.dates import DatesFile
from muse.together.infra.notes import NotesFiles
from muse.together.infra.sql import SqlLetters, SqlOurs, SqlPins, SqlSecrets, SqlTogether
from muse.together.service import Dates, Following, Letters, Live, Notes, Ours, Pins, Secrets


class ContextsProvider(Provider):
    scope = Scope.REQUEST

    sessions = provide(SqlSessions, provides=SessionStore)
    login = provide(LoginService)
    session = provide(SessionService)

    catalog_queries = provide(SqlCatalog, provides=CatalogQueries, scope=Scope.APP)
    catalog = provide(Catalog, scope=Scope.APP)

    @provide(scope=Scope.APP)
    def library(self, settings: Settings) -> LibraryState:
        paths = settings.paths
        return LibraryFiles(paths.catalog_db, paths.playlists_dir, paths.covers_dir)

    @provide(scope=Scope.APP)
    def playlists(self, settings: Settings) -> PlaylistSource:
        return PlaylistFiles(settings.paths.playlists_dir)

    @provide(scope=Scope.APP)
    def library_root(self, settings: Settings) -> LibraryRoot:
        return LibraryRoot.at(settings.paths.library_dir)

    @provide(scope=Scope.APP)
    def track_storage(self, settings: Settings, root: LibraryRoot) -> TrackStorage:
        return TrackFiles(settings.paths.library_dir, root)

    artwork_library = provide(SqlArtworkLibrary, provides=ArtworkLibrary, scope=Scope.APP)

    @provide(scope=Scope.APP)
    def artwork_files(self, settings: Settings) -> ArtworkFiles:
        paths = settings.paths
        return LocalArtworkFiles(paths.covers_dir, paths.artists_dir, paths.thumbs_dir)

    @provide(scope=Scope.APP)
    def artwork_stores(self, client: httpx.AsyncClient) -> ArtworkStores:
        return OnlineStores(client, anyio.sleep)

    @provide(scope=Scope.APP)
    def artwork(
        self,
        catalog: Catalog,
        library: ArtworkLibrary,
        files: ArtworkFiles,
        stores: ArtworkStores,
    ) -> Artwork:
        return Artwork(catalog, library, files, stores, anyio.sleep)

    play_store = provide(SqlPlays, provides=PlayRepository)
    like_store = provide(SqlLikes, provides=LikeRepository)
    plays = provide(Plays)
    likes = provide(Likes)

    subscription_store = provide(SqlSubscriptions, provides=SubscriptionRepository)
    notifier = provide(Notifier)
    push_subscriptions = provide(PushSubscriptions)
    push_reactions = provide(PushReactions)

    pet_store = provide(SqlPets, provides=PetRepository)
    quest_store = provide(SqlQuests, provides=QuestRepository)
    pet_keeper = provide(PetKeeper)
    quests = provide(Quests)
    pet_views = provide(PetViews)
    pet_care = provide(PetCare)
    pet_reactions = provide(PetReactions)
    quest_albums = provide(QuestAlbums)
    pet_pushes = provide(PetPushes)
    hunger_watch = provide(HungerWatch)

    together_ledger = provide(SqlTogether, provides=TogetherLedger)
    letter_box = provide(SqlLetters, provides=LetterBox)
    ours_list = provide(SqlOurs, provides=OursList)
    secret_box = provide(SqlSecrets, provides=SecretBox)
    pin_board = provide(SqlPins, provides=PinBoard)
    live = provide(Live)
    following = provide(Following)
    letters = provide(Letters)
    ours = provide(Ours)
    notes = provide(Notes)
    secrets = provide(Secrets)
    pins = provide(Pins)
    dates = provide(Dates)

    @provide(scope=Scope.APP)
    def notes_source(self, settings: Settings) -> NotesSource:
        return NotesFiles(settings.paths.notes_dir)

    @provide(scope=Scope.APP)
    def dates_source(self, settings: Settings) -> DatesSource:
        return DatesFile(settings.paths.dates_file)

    play_stats = provide(SqlStats, provides=PlayStats)
    listening_stats = provide(ListeningStats)

    @provide(scope=Scope.APP)
    def lyrics_source(self, client: httpx.AsyncClient) -> LyricsSource:
        return Lrclib(client)

    lyrics = provide(Lyrics, scope=Scope.APP)

    radio_model_cache = provide(RadioModelCache, scope=Scope.APP)
    radio_models = provide(RadioModels, scope=Scope.APP)
    listening_history = provide(SqlListeningHistory, provides=ListeningHistory)
    genre_tracks = provide(SqlGenreTracks, provides=GenreTracks)
    vector_source = provide(SqlVectors, provides=VectorSource, scope=Scope.APP)
    vector_index_cache = provide(VectorIndexCache, scope=Scope.APP)
    vector_indexes = provide(VectorIndexes, scope=Scope.APP)
    mood_memo = provide(MoodMemo, scope=Scope.APP)
    radio = provide(Radio)
    mixes = provide(Mixes)
    moods = provide(Moods)

    @provide(scope=Scope.APP)
    def text_encoder(self, settings: Settings, client: httpx.AsyncClient) -> TextEncoder:
        return HttpTextEncoder(client, settings.moods.text_url)

    daily_store = provide(SqlDailyStore, provides=DailyStore)
    daily_taste = provide(SqlTasteSource, provides=TasteSource)
    daily_playlists = provide(DailyPlaylists)
    daily_maker = provide(DailyMaker)

    home_queries = provide(SqlHome, provides=HomeQueries)
    continue_cards = provide(ContinueCards)
    home_shelves = provide(Shelves)
    home_feed = provide(HomeFeed)
    home_refresh = provide(HomeRefresh)

    hls_store = provide(SqlHls, provides=HlsStore)
    hls_songs = provide(SongCache, scope=Scope.APP)
    hls_sessions = provide(HlsSessions)

    player_log_service = provide(PlayerLogService)

    @provide(scope=Scope.APP)
    def player_log(self, settings: Settings) -> PlayerLog:
        return PlayerLogFile(settings.paths.player_log)
