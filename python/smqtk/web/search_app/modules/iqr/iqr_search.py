"""
Video Search blueprint
"""

import base64
import json
import logging
import multiprocessing
import os
import os.path as osp
import random

import flask
import PIL.Image

from smqtk.algorithms.content_description import get_descriptors
from smqtk.representation import get_data_set_impls
from smqtk.representation.data_element_impl.file_element import DataFileElement
from smqtk.iqr_control import IqrController, IqrSession
from smqtk.algorithms.iqr_index import get_iqr_index
from smqtk.algorithms.similarity_index import get_similarity_nn
from smqtk.utils import Configurable
from smqtk.utils import plugin
from smqtk.utils.preview_cache import PreviewCache
from smqtk.web.search_app.modules.file_upload import FileUploadMod

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class IqrSearch (flask.Blueprint, Configurable):
    """
    IQR Search Tab blueprint

    Components:
    * Data-set, from which base media data is provided
    * Descriptor generator, which provides descriptor generation services for
        user uploaded data.
    * SimilarityIndex, from which descriptors are queried from user input data.
        This index should contain descriptors that were generated by the same
        descriptor generator configuration above (same dimensionality, etc.).
    * IqrIndex, which is populated by an initial query, and then iterated over
        within the same user session. A new instance and model is generated
        every time a new session is created (or new data is uploaded by the
        user).

    """

    @classmethod
    def get_default_config(cls):
        return {
            "name": "Default IQR Search",
            "url_prefix": "/iqr_search",
            "data_set": plugin.make_config(get_data_set_impls),
            "descriptor_generator": plugin.make_config(get_descriptors),
            "similarity_index": plugin.make_config(get_similarity_nn),
            "iqr_index": plugin.make_config(get_iqr_index),
        }

    # noinspection PyMethodOverriding
    @classmethod
    def from_config(cls, config, parent_app):
        """
        Instantiate a new instance of this class given the configuration
        JSON-compliant dictionary encapsulating initialization arguments.

        :param config: JSON compliant dictionary encapsulating
            a configuration.
        :type config: dict

        :param parent_app: Parent containing flask app instance
        :type parent_app: smqtk.web.search_app.app.search_app

        :return: Constructed instance from the provided config.
        :rtype: IqrSearch

        """
        name = config['name']
        url_prefix = config['url_prefix']

        # construct nested objects via configurations
        data_set = plugin.from_plugin_config(config['data_set'],
                                             get_data_set_impls)
        descr_generator = plugin.from_plugin_config(config['descriptor_generator'],
                                                    get_descriptors)
        sim_index = plugin.from_plugin_config(config['similarity_index'],
                                              get_similarity_nn)
        iqr_index = plugin.from_plugin_config(config['iqr_index'],
                                              get_iqr_index)
        return cls(name, parent_app, data_set, descr_generator, sim_index,
                   iqr_index, url_prefix)

    def get_config(self):
        # TODO: Fill in recorded instances of algorithms
        #       - or stored algorithm configs
        return {
            'name': self.name,
            'url_prefix': self.url_prefix,
            'data_set': plugin.to_plugin_config(),
            'descriptor_generator': plugin.to_plugin_config(),
            'similarity_index': plugin.to_plugin_config(),
            'iqr_index': plugin.to_plugin_config(),
        }

    def __init__(self, name, parent_app, data_set,
                 descriptor_type, indexer_type,
                 url_prefix=None):
        """
        Initialize a generic IQR Search module with a single descriptor and
        indexer.

        :param name: Name of this blueprint instance
        :type name: str

        :param parent_app: Parent containing flask app instance
        :type parent_app: smqtk.web.search_app.app.search_app

        :param data_set: Data set to work over
        :type data_set: SMQTK.representation.DataSet

        :param descriptor_type: Feature Descriptor type string
        :type descriptor_type: str

        :param indexer_type: indexer type string
        :type indexer_type: str

        :param url_prefix: Web address prefix for this blueprint.
        :type url_prefix: str

        :raises ValueError: Invalid Descriptor or indexer type

        """
        # make sure URL prefix starts with a slash
        if not url_prefix.startswith('/'):
            url_prefix = '/' + url_prefix

        super(IqrSearch, self).__init__(
            name, import_name=__name__,
            static_folder=os.path.join(SCRIPT_DIR, "static"),
            template_folder=os.path.join(SCRIPT_DIR, "templates"),
            url_prefix=url_prefix
        )

        # Make sure that the configured descriptor/indexer types exist, as
        # we as their system configuration sections
        if descriptor_type not in ContentDescriptorConfiguration.available_labels():
            raise ValueError("'%s' not a valid descriptor type" % descriptor_type)
        if indexer_type not in IndexerConfiguration.available_labels():
            raise ValueError("'%s' not a valid indexer type" % indexer_type)

        self._parent_app = parent_app
        self._data_set = data_set
        self._fd_type_str = descriptor_type
        self._idxr_type_str = indexer_type

        self._explicit_uids = set()
        self._explicit_uids_lock = multiprocessing.RLock()
        # TODO: Read in dict from save file

        # Uploader Sub-Module
        self.upload_work_dir = os.path.join(self.work_dir, "uploads")
        self.mod_upload = FileUploadMod('%s_uploader' % self.name, parent_app,
                                        self.upload_work_dir,
                                        url_prefix='/uploader')
        self.register_blueprint(self.mod_upload)

        # IQR Session control
        # TODO: Move session management to database. Create web-specific
        #       IqrSession class that stores/gets its state directly from
        #       database.
        self._iqr_controller = IqrController()

        # structures for session ingest progress
        # Two levels: SID -> FID
        self._ingest_progress_locks = {}
        self._ingest_progress = {}

        # Preview Image Caching
        # TODO: Initialize this into static directory that is being served.
        self._preview_cache = PreviewCache(osp.join(self.work_dir, "Previews"))

        # Directory to write data for static viewing
        self._static_data_dir = os.path.join(self.static_folder, 'tmp_data')
        # Cache mapping of written static files for data elements
        self._static_cache = {}

        #
        # Routing
        #

        @self.route("/")
        @self._parent_app.module_login.login_required
        def index():
            r = {
                "module_name": self.name,
                "uploader_url": self.mod_upload.url_prefix,
                "uploader_post_url": self.mod_upload.upload_post_url(),
            }
            r.update(parent_app.nav_bar_content())
            # noinspection PyUnresolvedReferences
            return flask.render_template("iqr_search_index.html", **r)

        @self.route('/iqr_session_info', methods=["GET"])
        @self._parent_app.module_login.login_required
        def iqr_session_info():
            """
            Get information about the current IRQ session
            """
            with self.get_current_iqr_session() as iqrs:
                # noinspection PyProtectedMember
                return flask.jsonify({
                    "uuid": iqrs.uuid,
                    "positive_uids": tuple(iqrs.positive_ids),
                    "negative_uids": tuple(iqrs.negative_ids),
                    "extension_ingest_contents":
                        dict((uid, str(df))
                             for uid, df in iqrs.extension_ds.iteritems()),
                    "FeatureMemory": {
                    }
                })

        @self.route("/check_current_iqr_session")
        @self._parent_app.module_login.login_required
        def check_current_iqr_session():
            """
            Check that the current IQR session exists and is initialized.

            :rtype: {
                    success: bool
                }
            """
            # Getting the current IQR session ensures that one has been
            # constructed for the current session.
            with self.get_current_iqr_session():
                return flask.jsonify({
                    "success": True
                })

        @self.route('/iqr_ingest_file', methods=['POST'])
        @self._parent_app.module_login.login_required
        def iqr_ingest_file():
            """
            Ingest the file with the given UID, getting the path from the
            uploader.

            :return: status message
            :rtype: str

            """
            # TODO: Add status dict with a "GET" method branch for getting that
            #       status information.

            # Start the ingest of a FID when POST
            if flask.request.method == "POST":
                iqr_sess = self.get_current_iqr_session()
                fid = flask.request.form['fid']

                self.log.debug("[%s::%s] Getting temporary filepath from "
                               "uploader module", iqr_sess.uuid, fid)
                upload_filepath = self.mod_upload.get_path_for_id(fid)
                self.mod_upload.clear_completed(fid)

                # Extend session ingest -- modifying
                with iqr_sess:
                    self.log.debug("[%s::%s] Adding new file to extension "
                                   "ingest", iqr_sess.uuid, fid)
                    sess_upload = osp.join(iqr_sess.work_dir,
                                           osp.basename(upload_filepath))
                    os.rename(upload_filepath, sess_upload)
                    upload_data = DataFileElement(sess_upload)
                    iqr_sess.extension_ds.add_data(upload_data)

                # Compute feature for data -- non-modifying
                self.log.debug("[%s::%s] Computing feature for file",
                               iqr_sess.uuid, fid)
                feat = iqr_sess.descriptor.compute_descriptor(upload_data)

                # Extend indexer model with feature data -- modifying
                with iqr_sess:
                    self.log.debug("[%s::%s] Extending indexer model with "
                                   "feature", iqr_sess.uuid, fid)
                    iqr_sess.indexer.extend_model({upload_data.uuid(): feat})

                    # of course, add the new data element as a positive
                    iqr_sess.adjudicate((upload_data.uuid(),))

                return "Finished Ingestion"

        @self.route("/adjudicate", methods=["POST", "GET"])
        @self._parent_app.module_login.login_required
        def adjudicate():
            """
            Update adjudication for this session

            :return: {
                    success: <bool>,
                    message: <str>
                }
            """
            if flask.request.method == "POST":
                fetch = flask.request.form
            elif flask.request.method == "GET":
                fetch = flask.request.args
            else:
                raise RuntimeError("Invalid request method '%s'"
                                   % flask.request.method)

            pos_to_add = json.loads(fetch.get('add_pos', '[]'))
            pos_to_remove = json.loads(fetch.get('remove_pos', '[]'))
            neg_to_add = json.loads(fetch.get('add_neg', '[]'))
            neg_to_remove = json.loads(fetch.get('remove_neg', '[]'))

            self.log.debug("Adjudicated Positive{+%s, -%s}, Negative{+%s, -%s} "
                           % (pos_to_add, pos_to_remove,
                              neg_to_add, neg_to_remove))

            with self.get_current_iqr_session() as iqrs:
                iqrs.adjudicate(pos_to_add, neg_to_add,
                                pos_to_remove, neg_to_remove)
            return flask.jsonify({
                "success": True,
                "message": "Adjudicated Positive{+%s, -%s}, Negative{+%s, -%s} "
                           % (pos_to_add, pos_to_remove,
                              neg_to_add, neg_to_remove)
            })

        @self.route("/get_item_adjudication", methods=["GET"])
        @self._parent_app.module_login.login_required
        def get_adjudication():
            """
            Get the adjudication status of a particular result by ingest ID.

            This should only ever return a dict where one of the two, or
            neither, are labeled True.

            :return: {
                    is_pos: <bool>,
                    is_neg: <bool>
                }
            """
            ingest_uid = flask.request.args['uid']
            with self.get_current_iqr_session() as iqrs:
                return flask.jsonify({
                    "is_pos": ingest_uid in iqrs.positive_ids,
                    "is_neg": ingest_uid in iqrs.negative_ids
                })

        @self.route("/get_positive_uids", methods=["GET"])
        @self._parent_app.module_login.login_required
        def get_positive_uids():
            """
            Get a list of the positive ingest UIDs

            :return: {
                    uids: list of <int>
                }
            """
            with self.get_current_iqr_session() as iqrs:
                return flask.jsonify({
                    "uids": list(iqrs.positive_ids)
                })

        @self.route("/get_random_uids")
        @self._parent_app.module_login.login_required
        def get_random_uids():
            """
            Return to the client a list of all known dataset IDs but in a random
            order. If there is currently an active IQR session with elements in
            its extension ingest, then those IDs are included in the random
            list.

            :return: {
                    uids: list of int
                }
            """
            all_ids = self._data_set.uuids()
            with self.get_current_iqr_session() as iqrs:
                all_ids.update(iqrs.extension_ds.uuids())
            all_ids = list(all_ids)
            random.shuffle(all_ids)
            return flask.jsonify({
                "uids": all_ids
            })

        @self.route("/get_ingest_image_preview_data", methods=["GET"])
        @self._parent_app.module_login.login_required
        def get_ingest_item_image_rep():
            """
            Return the base64 preview image data for the data file associated
            with the give UID.
            """
            uid = flask.request.args['uid']

            info = {
                "success": True,
                "message": None,
                "is_explicit": None,
                "shape": None,  # (width, height)
                "data": None,
                "ext": None,
                "static_file_link": None,
            }

            #: :type: smqtk.representation.DataElement
            de = None
            if self._data_set.has_uuid(uid):
                de = self._data_set.get_data(uid)
                with self._explicit_uids_lock:
                    info["is_explicit"] = uid in self._explicit_uids
            else:
                with self.get_current_iqr_session() as iqrs:
                    if iqrs.extension_ds.has_uuid(uid):
                        de = iqrs.extension_ds.get_data(uid)
                        info["is_explicit"] = uid in self._explicit_uids

            if not de:
                info["success"] = False
                info["message"] = "UUID not part of the active data set!"
            else:
                # TODO: Have data-file return an HTML chunk for implementation
                #       defined visualization?
                img_path = self._preview_cache.get_preview_image(de)
                img = PIL.Image.open(img_path)
                info["shape"] = img.size
                with open(img_path, 'rb') as img_file:
                    info["data"] = base64.encodestring(img_file.read())
                info["ext"] = osp.splitext(img_path)[1].lstrip('.')

                if de.uuid() not in self._static_cache:
                    self._static_cache[de.uuid()] = \
                        de.write_temp(self._static_data_dir)
                info['static_file_link'] = 'static/' \
                    + os.path.relpath(self._static_cache[de.uuid()],
                                      self.static_folder)

            return flask.jsonify(info)

        @self.route("/mark_uid_explicit", methods=["POST"])
        @self._parent_app.module_login.login_required
        def mark_uid_explicit():
            """
            Mark a given UID as explicit in its containing ingest.

            :return: Success value of True if the given UID was valid and set
                as explicit in its containing ingest.
            :rtype: {
                "success": bool
            }
            """
            uid = flask.request.form['uid']
            self._explicit_uids.add(uid)
            # TODO: Save out dict

            return flask.jsonify({'success': True})

        @self.route("/iqr_refine", methods=["POST"])
        @self._parent_app.module_login.login_required
        def iqr_refine():
            """
            Classify current IQR session indexer, updating ranking for
            display.

            Fails gracefully if there are no positive[/negative] adjudications.

            Expected Args:
            """
            pos_to_add = json.loads(flask.request.form.get('add_pos', '[]'))
            pos_to_remove = json.loads(flask.request.form.get('remove_pos', '[]'))
            neg_to_add = json.loads(flask.request.form.get('add_neg', '[]'))
            neg_to_remove = json.loads(flask.request.form.get('remove_neg', '[]'))

            with self.get_current_iqr_session() as iqrs:
                try:
                    iqrs.refine(pos_to_add, neg_to_add,
                                pos_to_remove, neg_to_remove)
                    return flask.jsonify({
                        "success": True,
                        "message": "Completed refinement"
                    })
                except Exception, ex:
                    return flask.jsonify({
                        "success": False,
                        "message": "ERROR: %s: %s" % (type(ex).__name__,
                                                      ex.message)
                    })

        @self.route("/iqr_ordered_results", methods=['GET'])
        @self._parent_app.module_login.login_required
        def get_ordered_results():
            """
            Get ordered (UID, probability) pairs in between the given indices,
            [i, j). If j Is beyond the end of available results, only available
            results are returned.

            This may be empty if no refinement has yet occurred.

            Return format:
            {
                results: [ (uid, probability), ... ]
            }
            """
            with self.get_current_iqr_session() as iqrs:
                i = int(flask.request.args.get('i', 0))
                j = int(flask.request.args.get('j', len(iqrs.results)
                                               if iqrs.results else 0))
                return flask.jsonify({
                    "results": (iqrs.ordered_results or [])[i:j]
                })

        @self.route("/reset_iqr_session", methods=["POST"])
        @self._parent_app.module_login.login_required
        def reset_iqr_session():
            """
            Reset the current IQR session
            """
            with self.get_current_iqr_session() as iqrs:
                iqrs.reset()
                return flask.jsonify({
                    "success": True
                })

    def register_blueprint(self, blueprint, **options):
        """ Add sub-blueprint to a blueprint. """
        def deferred(state):
            if blueprint.url_prefix:
                blueprint.url_prefix = self.url_prefix + blueprint.url_prefix
            else:
                blueprint.url_prefix = self.url_prefix
            state.app.register_blueprint(blueprint, **options)

        self.record(deferred)

    @property
    def log(self):
        return logging.getLogger("smqtk.IQRSearch(%s)" % self.name)

    @property
    def work_dir(self):
        """
        :return: Common work directory for this instance.
        :rtype: str
        """
        return osp.join(self._parent_app.config['WORK_DIR'], "Web", "IQR",
                        self.name)

    def get_current_iqr_session(self):
        """
        Get the current IQR Session instance.

        :rtype: smqtk.IQR.iqr_session.IqrSession

        """
        with self._iqr_controller:
            sid = flask.session.sid
            if not self._iqr_controller.has_session_uuid(sid):
                sid_work_dir = osp.join(self.work_dir, sid)

                descriptor = ContentDescriptorConfiguration.new_inst(self._fd_type_str)
                indexer = IndexerConfiguration.new_inst(self._idxr_type_str)

                iqr_sess = IqrSession(sid_work_dir, descriptor, indexer, sid)
                self._iqr_controller.add_session(iqr_sess, sid)

                # If there are things already in our extension ingest, extend
                # the base indexer
                feat_map = \
                    descriptor.compute_descriptor_async(iqr_sess.extension_ds)
                indexer.extend_model(feat_map)

            return self._iqr_controller.get_session(sid)
