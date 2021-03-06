# -*- coding: utf-8 -*-

import locale
import logging
import os
import threading
from datetime import datetime as dt

import mutagen
from mutagen.easyid3 import EasyID3

from odoo import api, fields, models

# Pytaglib and Mutagen are supported for tag reading. Pytaglib is preferred as it seems less likely
# to send an exception in case of incorrect file.
try:
    import taglib
except ImportError:
    taglib = None


_logger = logging.getLogger(__name__)


class MusicFolderScan(models.TransientModel):
    _name = "oomusic.folder.scan"
    _description = "Music Folder scan log"

    ALLOWED_FILE_EXTENSIONS = {
        "aac",
        "caf",
        "flac",
        "m4a",
        "mp3",
        "mp4",
        "mpc",
        "oga",
        "ogg",
        "opus",
        "wav",
        "weba",
        "webm",
    }

    FIELDS_TO_CLEAN = {
        "name",
        "artist_id",
        "album_artist_id",
        "album_id",
        "disc",
        "year",
        "track_number",
        "track_total",
        "genre_id",
        "description",
        "composer",
        "performer_id",
        "copyright",
        "contact",
        "encoded_by",
    }

    MAP_ID3_FIELD = {
        "ALBUM": "album_id",
        "ALBUMARTIST": "album_artist_id",
        "ARTIST": "artist_id",
        "COMPOSER": "composer",
        "CONTACT": "contact",
        "COPYRIGHT": "copyright",
        "DATE": "year",
        "DESCRIPTION": "description",
        "DISCNUMBER": "disc",
        "ENCODED-BY": "encoded_by",
        "GENRE": "genre_id",
        "PERFORMER": "performer_id",
        "TITLE": "name",
        "TRACKNUMBER": "track_number",
        "TRACKTOTAL": "track_total",
    }

    def _lock_folder(self, folder_id):
        """
        Check if a folder is locked. If it is not locked, lock it. If it is locked, log an error.

        :param int folder_id: ID of the folder to scan
        :return bool: False if the folder was already locked, True if it was not
        """
        Folder = self.env["oomusic.folder"].browse([folder_id])
        if Folder.locked:
            _logger.error('"%s" is locked! It probably means that a scan is ongoing.', Folder.path)
            res = False
        else:
            Folder.write({"last_commit": dt.now(), "locked": True})
            res = True

        self.env.cr.commit()
        return res

    def _commit_or_flush(self):
        # Commit and close the transaction
        if not self.env.context.get("test_mode"):
            self.env.cr.commit()
        else:
            for model in [
                "oomusic.album",
                "oomusic.artist",
                "oomusic.folder",
                "oomusic.genre",
                "oomusic.track",
            ]:
                self.env[model].flush()

    def _clean_directory(self, path, user_id):
        """
        Clean a directory. It removes folders and tracks which are not on the disk anymore. This
        can potentially deletes the folder linked to the given path if the path doesn't exist
        anymore.

        :param str path: path of the folder to clean
        :param int user_id: ID of the user to whom belongs the folder
        """
        _logger.debug('Cleaning folder "%s"...', path)

        # List existing directories and files
        filelist = set(" ")
        folderlist = set(" ")
        for rootdir, dirnames, filenames in os.walk(path):
            folderlist.add(rootdir)
            for fn in filenames:
                fn_ext = fn.split(".")[-1]
                if fn_ext and fn_ext.lower() in self.ALLOWED_FILE_EXTENSIONS:
                    filelist.add(os.path.join(rootdir, fn))

        track_data = [
            (("id", "path"), "oomusic_folder", folderlist),
            (("id", "path"), "oomusic_track", filelist),
        ]

        # Cleaning part:
        # - select existing paths in table
        # - compare with paths actually used
        # - deletes the ones which are not used anymore
        for td in track_data:
            field_list = ",".join([x for x in td[0]])
            query = "SELECT " + field_list + " FROM " + td[1] + " WHERE user_id = " + str(user_id)
            self.env.cr.execute(query)
            res = self.env.cr.fetchall()
            to_clean = {r[1] for r in res if path in r[1]} - td[2]

            if to_clean:
                to_clean = {r[1]: r[0] for r in res if r[1] in to_clean}.values()
                self.env[td[1].replace("_", ".")].browse(to_clean).sudo().unlink()

    def _clean_tags(self, user_id):
        """
        Clean tags. It remove artists, albums, genres and playlist lines not listed in tracks
        anymore.

        :param int user_id: ID of the user to whom belongs the folder
        """
        _logger.debug('Cleaning tags for user_id "%s"...', user_id)

        # Select relevant data from oomusic tracks
        params = (user_id,)
        query = """
        SELECT artist_id, album_artist_id, performer_id, album_id, genre_id, id
            FROM oomusic_track
            WHERE user_id = %s;
        """
        self.env.cr.execute(query, params)
        res = self.env.cr.fetchall()
        artist_track = {r[0] for r in res} | {r[1] for r in res} | {r[2] for r in res}
        album_track = {r[3] for r in res}
        genre_track = {r[4] for r in res}
        track_id_track = {r[5] for r in res}

        track_data = [
            ("id", "oomusic_artist", artist_track),
            ("id", "oomusic_album", album_track),
            ("id", "oomusic_genre", genre_track),
            ("track_id", "oomusic_playlist_line", track_id_track),
        ]

        # Cleaning part:
        # - select existing ids in table
        # - compare with ids actually used in tracks
        # - deletes the ones which are not used anymore
        for td in track_data:
            query = "SELECT " + td[0] + " FROM " + td[1] + " WHERE user_id = " + str(user_id)
            self.env.cr.execute(query)
            res = self.env.cr.fetchall()
            to_clean = {r[0] for r in res} - td[2]

            if to_clean:
                self.env[td[1].replace("_", ".")].browse(to_clean).sudo().unlink()

    def _build_cache_global(self, folder_id, user_id):
        """
        Builds the cache for a given root folder. This avoids using the ORM cache which does not
        show the required performances for a large number of files. It caches information about
        albums, artists, folders and genres. Only the necessary data is set in cache.

        :param int folder_id: ID of the folder to scan
        :param int user_id: ID of the user to whom belongs the folder
        :return dict: content of the cache
        """
        _logger.debug('Building cache for folder_id "%s"...', folder_id)
        cache = {}
        cache["track"] = {}
        cache["user_id"] = user_id
        params = (user_id,)

        query = "SELECT name, folder_id, id FROM oomusic_album WHERE user_id = %s;"
        self.env.cr.execute(query, params)
        res = self.env.cr.fetchall()
        cache["album"] = {(r[0], r[1]): r[2] for r in res}

        query = "SELECT name, id FROM oomusic_artist WHERE user_id = %s;"
        self.env.cr.execute(query, params)
        res = self.env.cr.fetchall()
        cache["artist"] = {r[0]: r[1] for r in res}

        query = "SELECT path, id, last_modification FROM oomusic_folder WHERE user_id = %s;"
        self.env.cr.execute(query, params)
        res = self.env.cr.fetchall()
        cache["folder"] = {r[0]: (r[1], r[2] or 0) for r in res}

        query = "SELECT name, id FROM oomusic_genre WHERE user_id = %s;"
        self.env.cr.execute(query, params)
        res = self.env.cr.fetchall()
        cache["genre"] = {r[0]: r[1] for r in res}

        return cache

    def _build_cache_folder(self, folder_id, user_id, cache):
        """
        Builds the cache for a given folder. This avoids using the ORM cache which does not show
        the required performances for a large number of files. It caches information about tracks.
        Only the necessary data is set in cache.

        :param int folder_id: ID of the folder to scan
        :param int user_id: ID of the user to whom belongs the folder
        """
        params = (user_id, folder_id)
        query = """
            SELECT path, id, last_modification FROM oomusic_track
            WHERE user_id = %s AND folder_id = %s;
        """
        self.env.cr.execute(query, params)
        res = self.env.cr.fetchall()
        cache["track"] = {r[0]: (r[1], r[2]) for r in res}

    def _build_cache_write(self):
        """
        Prepare the cache for writing. To avoid using stored related/computed fields which impact
        scanning performances, we build a cache for writing.

        A typical example is the field `year` of an album. It could be defined as a related on
        `track_ids.year`, which by default takes the value of the first record. However, being able
        to sort and group albums by year is a required functionality for a end-user. Therefore, this
        field needs to be stored. This implies that, for every track added on an album, the field
        is recomputed, leading to a shitload of database queries, slowing down the whole process.

        :return dict: content of the cache, mostly empty
        """
        cache_write = {}
        cache_write["album"] = {}

        return cache_write

    def _manage_dir(self, rootdir, cache):
        """
        For a given directory, checks that it is already in the cache.
        - If not in the cache, create the associated folder
        - If in the cache, check the last modification date
        If the last modification date is older than the one recorded, the folder is skipped

        :param str rootdir: folder to check
        :param dict cache: reading cache
        :return bool: indicates if the folder scanning can be skipped
        """
        skip = False
        folder = cache["folder"].get(rootdir)
        mtime = int(os.path.getmtime(rootdir))

        if not folder:
            self._create_folder(rootdir, cache)
        elif folder[1] >= mtime:
            skip = True
        else:
            parent_dir = os.sep.join(rootdir.split(os.sep)[:-1])
            parent_dir = cache["folder"].get(parent_dir, [False])
            Folder = self.env["oomusic.folder"].browse(folder[0])
            Folder.write({"last_modification": mtime, "parent_id": parent_dir[0]})
        return skip

    def _create_folder(self, rootdir, cache):
        """
        Create the directory rootdir, and updates the cache.

        :param str rootdir: path of the folder to create
        :param dict cache: reading cache
        """
        parent_dir = os.sep.join(rootdir.split(os.sep)[:-1])
        parent_dir = cache["folder"].get(parent_dir)
        if parent_dir:
            mtime = int(os.path.getmtime(rootdir))
            vals = {
                "root": False,
                "path": rootdir,
                "parent_id": parent_dir[0],
                "last_modification": mtime,
                "user_id": cache["user_id"],
            }
            Folder = self.env["oomusic.folder"].create(vals)
            cache["folder"][rootdir] = (Folder.id, mtime)

    def _get_tags(self, file_path, tag=taglib):
        tag = tag or mutagen
        _logger.debug('Scanning file "%s"', file_path)
        try:
            song = tag.File(file_path)
            if not song:
                raise
        except:
            _logger.warning('Error while opening file "%s"', file_path, exc_info=True)
            return False, {}
        if tag.__name__ == "taglib":
            return song, song.tags
        else:
            try:
                song_tags = EasyID3(file_path) or {}
            except:
                song_tags = song.tags or {}

            song_tags = {k.upper(): v for k, v in song_tags.items()}
            return song, song_tags

    def _create_related(self, vals, cache, rootdir):
        """
        Create the related objects of the track: album, artist and genre. Updates the cache
        accordingly.

        :param dict vals: data of the track
        :param dict cache: reading cache
        :param str rootdir: path of the folder being scanned
        """
        MusicAlbum = self.env["oomusic.album"]
        MusicArtist = self.env["oomusic.artist"]
        MusicGenre = self.env["oomusic.genre"]

        if (
            vals.get("album_id")
            and (vals["album_id"], cache["folder"][rootdir][0]) not in cache["album"].keys()
        ):
            cache["album"][(vals["album_id"], cache["folder"][rootdir][0])] = MusicAlbum.create(
                {
                    "name": vals["album_id"],
                    "user_id": cache["user_id"],
                    "folder_id": cache["folder"][rootdir][0],
                }
            ).id
        if vals.get("artist_id") and vals["artist_id"] not in cache["artist"].keys():
            cache["artist"][vals["artist_id"]] = MusicArtist.create(
                {"name": vals["artist_id"], "user_id": cache["user_id"]}
            ).id
        if vals.get("album_artist_id") and vals["album_artist_id"] not in cache["artist"].keys():
            cache["artist"][vals["album_artist_id"]] = MusicArtist.create(
                {"name": vals["album_artist_id"], "user_id": cache["user_id"]}
            ).id
        if vals.get("performer_id") and vals["performer_id"] not in cache["artist"].keys():
            cache["artist"][vals["performer_id"]] = MusicArtist.create(
                {"name": vals["performer_id"], "user_id": cache["user_id"]}
            ).id
        if vals.get("genre_id") and vals["genre_id"] not in cache["genre"].keys():
            cache["genre"][vals["genre_id"]] = MusicGenre.create(
                {"name": vals["genre_id"], "user_id": cache["user_id"]}
            ).id

    def _replace_related_by_id(self, vals, cache, rootdir):
        """
        Replace the data of the track with the ID of the related object. In other words, convert the
        name of the album, artist or genre into athe corresponding ID.

        :param dict vals: data of the track
        :param dict cache: reading cache
        :param str rootdir: path of the folder being scanned
        """
        if vals.get("album_id"):
            vals["album_id"] = cache["album"][(vals["album_id"], cache["folder"][rootdir][0])]
        if vals.get("artist_id"):
            vals["artist_id"] = cache["artist"][vals["artist_id"]]
        if vals.get("album_artist_id"):
            vals["album_artist_id"] = cache["artist"][vals["album_artist_id"]]
        if vals.get("performer_id"):
            vals["performer_id"] = cache["artist"][vals["performer_id"]]
        if vals.get("genre_id"):
            vals["genre_id"] = cache["genre"][vals["genre_id"]]

    def _update_cache_write(self, vals, cache_write):
        """
        Update the write cache with additional information. For example adds the year, artist and
        genre to an album.

        :param dict vals: data of the track
        :param dict cache_write: writing cache
        """
        if vals.get("album_id") and vals["album_id"] not in cache_write["album"]:
            cache_write["album"][vals["album_id"]] = {
                "year": vals.get("year"),
                "artist_id": vals.get("album_artist_id") or vals.get("artist_id"),
                "genre_id": vals.get("genre_id"),
            }

    def _write_cache_write(self, cache_write):
        """
        Performs the write oopration of the write_cache

        :param dict cache_write: writing cache
        """
        MusicFolder = self.env["oomusic.album"]
        for album_id in cache_write["album"].keys():
            MusicFolder.browse([album_id]).write(cache_write["album"][album_id])

    def _scan_folder(self, folder_id):
        """
        The folder scanning method. It walks in all sub-directories of the folder. If the
        modification date is more recent than the recorded date, the directory is scanned.

        A file is scanned if these conditions are met:
        - the extension matches the allowed file extensions;
        - the last modification date is more recent than the recorded date, in order to update data
          of an existing record.
        During the scan, any new album or artists will be created as well.

        There is an arbitrary commit every 1000 tracks or 2 minutes, which should allow a regular
        update of the database.

        :param int folder_id: ID of the folder to scan
        """
        if locale.getlocale() == (None, None):
            _logger.warning(
                "It seems the locale of your system is not set correctly. "
                + "It might be resolved by setting the system variable LC_ALL to UTF-8."
            )
        with api.Environment.manage(), self.pool.cursor() as cr:
            time_start = dt.now()
            # As this function is in a new thread, open a new cursor because the existing one may be
            # closed
            if not self.env.context.get("test_mode"):
                self = self.with_env(self.env(cr))

                # Lock the folder. A new cursor is necessary right after since it is closed
                # explicitly. If the folder is locked, do nothing
                if not self._lock_folder(folder_id):
                    return {}

            MusicFolder = self.env["oomusic.folder"]
            MusicTrack = self.env["oomusic.track"]

            Folder = MusicFolder.browse([folder_id])
            if Folder.tag_analysis == "taglib" and taglib:
                tag = taglib
            else:
                tag = mutagen

            # Clean-up the DB before actual scan
            self._clean_directory(Folder.path, Folder.user_id.id)
            if not Folder.exists():
                if not self.env.context.get("test_mode"):
                    self.env.cr.commit()
                return {}

            # Build the cache
            # - cache is used for read/search, i.e. avoid reading/searching same info several times
            # - cache_write is used for writing tracks info on other models and avoid stored
            #   related/computed fields
            cache = self._build_cache_global(Folder.id, Folder.user_id.id)
            cache_write = self._build_cache_write()
            i = 0

            # Start scanning
            time_commit = time_start
            for rootdir, dirnames, filenames in os.walk(Folder.path):
                _logger.debug('Scanning folder "%s"...', rootdir)

                # If the folder is in cache, it means we'll have to fetch the track data. We check
                # now since _manage_dir will add a missing folder in the cache.
                build_cache_folder = False
                if rootdir in cache["folder"]:
                    build_cache_folder = True

                skip = self._manage_dir(rootdir, cache)
                if skip:
                    continue

                # Complete the cache with track data
                if build_cache_folder:
                    self._build_cache_folder(cache["folder"][rootdir][0], Folder.user_id.id, cache)

                for fn in filenames:
                    # Check file extension
                    fn_ext = fn.split(".")[-1]
                    if fn_ext and fn_ext.lower() not in self.ALLOWED_FILE_EXTENSIONS:
                        continue

                    # Skip file if already in DB
                    fn_path = os.path.join(rootdir, fn)
                    mtime = int(os.path.getmtime(fn_path))
                    if fn_path in cache["track"].keys() and cache["track"][fn_path][1] >= mtime:
                        continue

                    # Get tags
                    song, song_tags = self._get_tags(os.path.join(rootdir, fn), tag=tag)
                    if song is False:
                        continue
                    vals = {f: False if "_id" in f else "" for f in self.FIELDS_TO_CLEAN}
                    if Folder.use_tags:
                        vals.update(
                            {
                                self.MAP_ID3_FIELD[k]: v[0]
                                for k, v in song_tags.items()
                                if v and k in self.MAP_ID3_FIELD.keys()
                            }
                        )

                    # Create new album, artist or genre, and update the cache
                    self._create_related(vals, cache, rootdir)

                    # Replace album, artist or genre by ID
                    self._replace_related_by_id(vals, cache, rootdir)

                    # Add missing fields
                    if tag.__name__ == "taglib":
                        vals["duration"] = song.length
                        vals["bitrate"] = song.bitrate
                    else:
                        vals["duration"] = int(song.info.length)
                        # 'bitrate' is not available for all file types
                        if hasattr(song.info, "bitrate"):
                            vals["bitrate"] = round((song.info.bitrate or 0) / 1000.0)
                        else:
                            vals["bitrate"] = 0
                    vals["duration_min"] = float(vals["duration"]) / 60
                    vals["size"] = (os.path.getsize(fn_path) or 0.0) / (1024.0 * 1024.0)
                    vals["path"] = fn_path
                    vals["last_modification"] = mtime
                    vals["root_folder_id"] = folder_id
                    vals["folder_id"] = cache["folder"][rootdir][0]
                    vals["user_id"] = cache["user_id"]
                    if not vals["name"]:
                        vals["name"] = fn
                    try:
                        vals["track_number_int"] = (
                            int(vals["track_number"].split("/")[0]) if vals["track_number"] else 0
                        )
                    except ValueError:
                        _logger.warning(
                            "Could not convert track number '%s' to integer", vals["track_number"]
                        )
                        vals["track_number_int"] = 0

                    # Create the track. No need to insert a new track in the cache, since we won't
                    # scan it during the process.
                    if fn_path in cache["track"].keys():
                        Track = MusicTrack.browse(cache["track"][fn_path][0])
                        Track.write(vals)
                        if self.env.context.get("test_mode"):
                            Track.flush()
                    else:
                        Track = MusicTrack.create(vals)

                    # Update writing cache
                    self._update_cache_write(vals, cache_write)

                    # Commit every 1000 tracks or 2 minutes
                    i = i + 1
                    if i % 1000 == 0 or (dt.now() - time_commit).total_seconds() > 120:
                        # Empty cache_write, so user already sees the album-related additional info
                        self._write_cache_write(cache_write)
                        cache_write = self._build_cache_write()
                        time_commit = dt.now()
                        Folder.write({"last_commit": time_commit, "locked": True})

                        # Commit and close the transaction
                        self._commit_or_flush()

            # Final stuff to write and tags cleaning
            self._write_cache_write(cache_write)
            if Folder.exists():
                if Folder.last_scan:
                    self._commit_or_flush()
                    self._clean_tags(Folder.user_id.id)
                Folder.write(
                    {
                        "last_scan": fields.Datetime.now(),
                        "last_scan_duration": round((dt.now() - time_start).total_seconds()),
                        "last_commit": dt.now(),
                        "locked": False,
                    }
                )
            self._commit_or_flush()
            if self.env.context.get("test_mode"):
                self.invalidate_cache()
            _logger.debug('Scan of folder_id "%s" completed!', folder_id)
            return {}

    def scan_folder_th(self, folder_id):
        """
        This is the method used to scan a oomusic folder with a new thread. Only one folder should be
        scanned at a time, since different folders can share album, artist and genre data.

        To improve scanning speed, two parameters are set in the context:
        - `recompute`: prevents calculating non-stored calculated fields
        - `prefetch_fields`: deactivate prefetching

        :param int folder_id: ID of the folder to scan
        """
        threaded_scan = threading.Thread(
            target=self.sudo().with_context(recompute=False, prefetch_fields=False)._scan_folder,
            args=(folder_id,),
        )
        threaded_scan.start()
