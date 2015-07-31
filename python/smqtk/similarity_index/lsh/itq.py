"""
Home of IQR LSH implementation based on UNC Chapel Hill paper / sample code.
"""
__author__ = 'purg'

import cPickle
import heapq
import os.path as osp
import numpy
import numpy.matlib

from smqtk.similarity_index import (
    SimilarityIndex,
    SimilarityIndexStateLoadError,
    SimilarityIndexStateSaveError,
)
from smqtk.similarity_index.lsh.code_index.memory import MemoryCodeIndex
from smqtk.utils import (
    bit_utils,
    distance_functions,
    safe_create_dir,
    SimpleTimer
)


class ITQSimilarityIndex (SimilarityIndex):
    """
    Nearest neighbor implementation using Iterative Quantization (ITQ), a method
    to convert a descriptor (e.g. 4000-dim vector) to few bits (e.g. 64 bits).

    The method first appeared in
    ```
    Yunchao Gong and Svetlana Lazebnik. Iterative Quantization: A Procrustes
    Approach to Learning Binary Codes. In CVPR 2011.
    ```
    It was originally implemented in Matlab by Yunchao Gong
    (yunchao@cs.unc.edu).

    It may be the case that there the given index instance is not empty. In this
    case, there is an existing computed index, but no internal state yet for
    this ITQ index.

    IMPORTANT:
        For consistency, we treat bit vectors such that the bit at index 0 is
        considered the most significant bit.

    """

    @classmethod
    def is_usable(cls):
        # Internal implementation, so no dependencies besides things like numpy,
        # which if we don't have, nothing will work.
        return True

    def __init__(self, index_factory=lambda: MemoryCodeIndex(), bit_length=8,
                 itq_iterations=50, distance_method='cosine', random_seed=None):
        """
        Initialize ITQ similarity index instance (not the index itself).

        :param index_factory: Method to produce a new code index instance. By
            default, we use an in-memory index.
        :type index_factory: ()->smqtk.similarity_index.lsh.code_index.CodeIndex

        :param bit_length: Number of bits used to represent descriptors (hash
            code). This must be greater than 0.
        :type bit_length: int

        :param itq_iterations: Number of iterations for the ITQ algorithm to
            perform. This must be greater than 0.
        :type itq_iterations: int

        :param distance_method: String label of distance method to use. This
            must one of the following:
                - "euclidean": Simple euclidean distance between two descriptors
                    (L2 norm).
                - "cosine": Cosine angle distance/similarity between two
                    descriptors.
                - "hik": Histogram intersection distance between two
                    descriptors.
        :type distance_method: str

        :param random_seed: Integer to use as the random number generator seed.
        :type random_seed: int

        """
        assert bit_length > 0, "Must be given a bit length greater than 1 " \
                               "(one)!"
        assert itq_iterations > 0, "Must be given a number of iterations " \
                                   "greater than 1 (one)!"

        # Index save/load directory and standard saved filepath
        self._save_file = "itq_index.pickle"

        # Number of bits we convert descriptors into
        self._bit_len = int(bit_length)
        # Number of iterations ITQ performs
        self._itq_iter_num = itq_iterations
        # Optional fixed random seed
        self._rand_seed = None if random_seed is None else int(random_seed)

        # Vector of mean feature values. Centers "train" set, used to "center"
        # additional descriptors when computing nearest neighbors.
        #: :type: numpy.core.multiarray.ndarray
        self._mean_vector = None

        # rotation matrix of shape [d, b], found by ITQ process, to use to
        # transform new descriptors into binary hash decision vector.
        #: :type: numpy.core.multiarray.ndarray[float] | None
        self._r = None

        # Function to produce a new CodeIndex implementation instance
        self._index_factory = index_factory

        # Hash table mapping small-codes to a list of DescriptorElements mapped
        # by that code
        #: :type: smqtk.similarity_index.lsh.code_index.CodeIndex
        self._code_index = None

        self._dist_method = distance_method
        self._dist_func = self._get_dist_func(distance_method)

    @staticmethod
    def _get_dist_func(distance_method):
        """
        Return appropriate distance function given a string label
        """
        if distance_method == "euclidean":
            #: :type: (ndarray, ndarray) -> ndarray
            return distance_functions.euclidean_distance
        elif distance_method == "cosine":
            # Inverse of cosine similarity function return
            #: :type: (ndarray, ndarray) -> ndarray
            return lambda i, j: 1.0 - distance_functions.cosine_similarity(i, j)
        elif distance_method == 'hik':
            #: :type: (ndarray, ndarray) -> ndarray
            return distance_functions.histogram_intersection_distance
        else:
            raise ValueError("Invalid distance method label. Must be one of "
                             "['euclidean' | 'cosine' | 'hik']")

    def count(self):
        """
        :return: Number of elements in this index.
        :rtype: int
        """
        if self._code_index:
            return self._code_index.count()
        else:
            return 0

    def _find_itq_rotation(self, v, n_iter):
        """
        Finds a rotation of the PCA embedded data. Number of iterations must be
        greater than 0.

        This is equivalent to the ITQ function from UNC-CH's implementation.

        :param v: 2D numpy array, n*c PCA embedded data, n is the number of data
            elements and c is the code length.
        :type v: numpy.core.multiarray.ndarray

        :param n_iter: max number of iterations, 50 is usually enough
        :type n_iter: int

        :return: [b, r]
           b: 2D numpy array, n*c binary matrix,
           r: 2D numpy array, the c*c rotation matrix found by ITQ
        :rtype: numpy.core.multiarray.ndarray, numpy.core.multiarray.ndarray

        """
        # initialize with an orthogonal random rotation
        bit = v.shape[1]
        r = numpy.random.randn(bit, bit)
        u11, s2, v2 = numpy.linalg.svd(r)
        r = u11[:, :bit]

        # ITQ to find optimal rotation
        self._log.debug("ITQ iterations to determine optimal rotation: %d",
                        n_iter)
        for i in range(n_iter):
            self._log.debug("ITQ iter %d", i + 1)
            z = numpy.dot(v, r)
            ux = numpy.ones(z.shape) * (-1)
            ux[z >= 0] = 1
            c = numpy.dot(ux.transpose(), v)
            ub, sigma, ua = numpy.linalg.svd(c)
            r = numpy.dot(ua, ub.transpose())

        # Make B binary matrix using final rotation matrix
        #   - original code returned B transformed by second to last rotation
        #       matrix, there by returning, de-synchronized matrices
        #   - Recomputing Z here so as to generate up-to-date B for the final
        #       rotation matrix computed.
        # TODO: Could move this step up one level and just return rotation mat
        z = numpy.dot(v, r)
        b = numpy.zeros(z.shape, dtype=numpy.uint8)
        b[z >= 0] = True

        return b, r

    def build_index(self, descriptors):
        """
        Build the index over the descriptor data elements.

        Subsequent calls to this method should rebuild the index, not add to it.

        The first part of this method is equivalent to the compressITQ function
        from UNC-CH's implementation.

        :raises ValueError: No data available in the given iterable.

        :param descriptors: Iterable of descriptor elements to build index over.
        :type descriptors: collections.Iterable[smqtk.data_rep.DescriptorElement]

        """
        self._log.debug("Using %d length bit-vectors", self._bit_len)

        # TODO: Sub-sample down descriptors to use for PCA + ITQ
        #       - Harry was also working on an iterative training approach so
        #           that we only have to have a limited number of vectors in
        #           memory at a time.
        if self._rand_seed:
            numpy.random.seed(self._rand_seed)

        with SimpleTimer("Creating descriptor matrix", self._log.info):
            x = []
            #: :type: list[smqtk.data_rep.DescriptorElement]
            descr_cache = []
            for d in descriptors:
                descr_cache.append(d)
                x.append(d.vector())
            if not x:
                raise ValueError("No descriptors given!")
            x = numpy.array(x)

        with SimpleTimer("Centering data", self._log.info):
            # center the data, VERY IMPORTANT for ITQ to work
            self._mean_vector = numpy.mean(x, axis=0)
            # x = x - numpy.matlib.repmat(self._mean_vector, x.shape[0], 1)
            x -= self._mean_vector

        # PCA
        with SimpleTimer("Computing PCA transformation", self._log.info):
            # numpy and matlab observation format is flipped, thus added
            # transpose
            self._log.debug("-- covariance")
            c = numpy.cov(x.transpose())

            # Direct translation
            # - eigen vectors are the columns of ``pc``
            self._log.debug('-- linalg.eig')
            l, pc = numpy.linalg.eig(c)
            # ordered by greatest eigenvalue magnitude, keeping top ``bit_len``
            self._log.debug('-- top pairs')
            top_pairs = sorted(zip(l, pc.transpose()),
                               key=lambda p: p[0],
                               reverse=1
                               )[:self._bit_len]

            # # Harry translation -- Uses singular values / vectors, not eigen
            # # - singular vectors are the rows of pc
            # pc, l, _ = numpy.linalg.svd(c)
            # top_pairs = sorted(zip(l, pc),
            #                    key=lambda p: p[0],
            #                    reverse=1
            #                    )[:self._bit_len]

            # Eigen-vectors of top ``bit_len`` magnitude eigenvalues
            self._log.debug("-- top vector extraction")
            pc_top = numpy.array([p[1] for p in top_pairs]).transpose()
            self._log.debug("-- transform centered data by PC matrix")
            xx = numpy.dot(x, pc_top)

        # ITQ to find optimal rotation.
        #   `c` is the output codes for matrix `x`
        #   `r` is the rotation found by ITQ
        with SimpleTimer("Performing ITQ to find optimal rotation",
                         self._log.info):
            c, self._r = self._find_itq_rotation(xx, self._itq_iter_num)
            # De-adjust rotation with PC vector
            self._r = numpy.dot(pc_top, self._r)

        # Populating small-code hash-table
        #   - Converting bit-vectors proved faster than creating new codes over
        #       again (~0.01s vs ~0.04s for 80 vectors).
        with SimpleTimer("Converting bitvectors into small codes",
                         self._log.info):
            self._code_index = self._index_factory()
            self._code_index.add_many_descriptors(
                (bit_utils.bit_vector_to_int(c[i]), descr_cache[i])
                for i in xrange(c.shape[0])
            )
        # NOTE: If a sub-sampling effect is implemented above, this will have to
        #       change to querying for descriptor vectors individually since the
        #       ``c`` matrix will not encode all descriptors at that point. This
        #       will be slower unless we think of something else. Could probably
        #       map the small code generation function by bringing it outside of
        #       the class.

    def save_index(self, dir_path):
        """
        Save the current index state to a given location.

        This will overwrite a previously saved state given the same
        configuration.

        :raises SimilarityIndexStateSaveError: Unable to save the current index
            state for some reason.

        :param dir_path: Path to the directory to save the index to.
        :type dir_path: str

        """
        if self._r is None:
            raise SimilarityIndexStateSaveError("No index build yet to save.")

        state = {
            "bit_len": self._bit_len,
            "itq_iter": self._itq_iter_num,
            "rand_seed": self._rand_seed,
            "mean_vector": self._mean_vector,
            "rotation": self._r,
            "code_index": self._code_index,  # should be picklable
            "distance_method": self._dist_method
        }

        safe_create_dir(dir_path)
        save_file = osp.join(dir_path, self._save_file)
        with open(save_file, 'wb') as f:
            cPickle.dump(state, f)

    def load_index(self, dir_path):
        """
        Load a saved index state from a given location.

        :raises SimilarityIndexStateLoadError: Could not load index state.

        :param dir_path: Path to the directory to load the index to.
        :type dir_path: str

        """
        save_file = osp.join(dir_path, self._save_file)

        if not osp.isfile(save_file):
            raise SimilarityIndexStateLoadError("Expected safe file not found: "
                                                "%s" % save_file)

        with open(save_file, 'rb') as f:
            state = cPickle.load(f)

        self._bit_len = state['bit_len']
        self._itq_iter_num = state['itq_iter']
        self._rand_seed = state['rand_seed']
        self._mean_vector = state['mean_vector']
        self._r = state['rotation']
        self._code_index = state['code_index']
        self._dist_method = state['distance_method']
        self._dist_func = self._get_dist_func(self._dist_method)

    def get_small_code(self, descriptor):
        """
        Generate the small-code for the given descriptor.

        This only works if we have an index loaded, meaning we have a rotation
        matrix.

        :param descriptor: Descriptor to generate the small code for.
        :type descriptor: smqtk.data_rep.DescriptorElement

        :return: The descriptor's vector, the n-bit vector, and the compacted
            N-bit small-code as an integer.
        :rtype: numpy.core.multiarray.ndarray[float],
                numpy.core.multiarray.ndarray[numpy.uint8],
                int

        """
        v = descriptor.vector()
        z = numpy.dot(v - self._mean_vector, self._r)
        b = numpy.zeros(z.shape, dtype=numpy.uint8)
        b[z >= 0] = 1
        return v, b, bit_utils.bit_vector_to_int(b)

    def nn(self, d, n=1):
        """
        Return the nearest `N` neighbors to the given descriptor element.

        :param d: Descriptor element to compute the neighbors of.
        :type d: smqtk.data_rep.DescriptorElement

        :param n: Number of nearest neighbors to find.
        :type n: int

        :return: Tuple of nearest N DescriptorElement instances, and a tuple of
            the distance values to those neighbors.
        :rtype: (tuple[smqtk.data_rep.DescriptorElement], tuple[float])

        """
        d_vec, _, d_sc = self.get_small_code(d)

        # Extract the `n` nearest codes to the code of the query descriptor
        # - a code may associate with multiple hits, but its a safe assumption
        #   that if we get the top `n` codes, which exist because there is at
        #   least one element in association with it,
        code_set = self._code_index.codes()
        # TODO: Optimize this step
        #: :type: list[int]
        near_codes = \
            heapq.nsmallest(n, code_set,
                            lambda e:
                                distance_functions.hamming_distance(d_sc, e)
                            )

        # Collect descriptors from subsequently farther away bins until we have
        # >= `n` descriptors, which we will more finely sort after this.
        #: :type: list[smqtk.data_rep.DescriptorElement]
        neighbors = []
        termination_count = min(n, self.count())
        for nc in near_codes:
            neighbors.extend(self._code_index.get_descriptors(nc))
            # Break out if we've collected >= `n` descriptors, as descriptors
            # from more distance codes are likely to not be any closer.
            if len(neighbors) >= termination_count:
                break

        # Compute fine-grain distance measurements for collected elements + sort
        distances = []
        for d_elem in neighbors:
            distances.append(self._dist_func(d_vec, d_elem.vector()))

        ordered = sorted(zip(distances, neighbors), key=lambda p: p[0])
        distances, neighbors = zip(*ordered)

        return neighbors[:n], distances[:n]
