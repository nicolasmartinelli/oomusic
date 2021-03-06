# -*- coding: utf-8 -*-

from . import test_common


class TestOomusicArtist(test_common.TestOomusicCommon):
    def test_00_action_add_to_playlist(self):
        """
        Test adding an artist to a playlist
        """
        self.FolderScanObj.with_context(test_mode=True)._scan_folder(self.Folder.id)

        artist1 = self.ArtistObj.search([("name", "=", "Artist1")])
        artist2 = self.ArtistObj.search([("name", "=", "Artist2")])
        playlist = self.PlaylistObj.search([("current", "=", True)], limit=1)

        # Check data prior any action
        self.assertEqual(len(artist1), 1)
        self.assertEqual(len(artist2), 1)
        self.assertEqual(len(playlist), 1)

        # Add Artist1
        artist1.action_add_to_playlist()
        self.assertEqual(
            playlist.mapped("playlist_line_ids").mapped("track_id").mapped("name"),
            [u"Song3", u"Song4", u"Song1", u"Song2"],
        )

        # Check no duplicate is created
        artist1.action_add_to_playlist()
        self.assertEqual(
            playlist.mapped("playlist_line_ids").mapped("track_id").mapped("name"),
            [u"Song3", u"Song4", u"Song1", u"Song2"],
        )

        # Purge and replace by Artist2
        artist2.with_context(purge=True).action_add_to_playlist()
        self.assertEqual(
            playlist.mapped("playlist_line_ids").mapped("track_id").mapped("name"),
            [u"Song5", u"Song6"],
        )

        self.cleanUp()
