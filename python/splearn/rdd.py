# -*- coding: utf-8 -*-

from pyspark.rdd import RDD

import numpy as np
import scipy.sparse as sp
import operator


def _pack_accumulated(accumulated):
    if len(accumulated) > 0 and sp.issparse(accumulated[0]):
        return sp.vstack(accumulated)
    else:
        return np.array(accumulated)


def _block_tuple(iterator, block_size=None):
    """Pack rdd of tuples as tuples of arrays or scipy.sparse matrices."""
    i = 0
    blocked_tuple = None
    for tuple_i in iterator:
        if blocked_tuple is None:
            blocked_tuple = tuple([] for _ in range(len(tuple_i)))

        if block_size is not None and i >= block_size:
            yield tuple(_pack_accumulated(x) for x in blocked_tuple)
            blocked_tuple = tuple([] for _ in range(len(tuple_i)))
            i = 0
        for x_j, x in zip(tuple_i, blocked_tuple):
            x.append(x_j)
        i += 1
    yield tuple(_pack_accumulated(x) for x in blocked_tuple)


def _block_collection(iterator, block_size=None):
    """Pack rdd with a specific collection constructor."""
    i = 0
    accumulated = []
    for a in iterator:
        if block_size is not None and i >= block_size:
            yield _pack_accumulated(accumulated)
            accumulated = []
            i = 0
        accumulated.append(a)
        i += 1
    yield _pack_accumulated(accumulated)


def block(rdd, block_size=None):
    """Block an RDD

    Parameters
    ----------

    rdd : RDD
        RDD of data points to block into either numpy arrays,
        scipy sparse matrices, or pandas data frames.
        Type of data point will be automatically inferred
        and blocked accordingly.

    block_size : int, optional, default None
        Size of each block (number of elements), if None all data points
        from each partition will be combined in a block.

    """
    try:
        entry = rdd.first()
    except IndexError:
        # empty RDD: do not block
        return rdd

    # do different kinds of block depending on the type
    if isinstance(entry, dict):
        return DictRDD(rdd.map(lambda x: x.items()), columns=entry.keys())
    elif isinstance(entry, tuple):
        return TupleRDD(rdd, block_size)
    else:  # Fallback to array packing
        return ArrayRDD(rdd, block_size)


# TODO: cache shape etc.


class ArrayRDD(object):

    def __init__(self, rdd, block_size=None):
        self.block_size = block_size
        if isinstance(rdd, ArrayRDD):
            self._rdd = rdd._rdd
        elif isinstance(rdd, RDD):
            if block_size is False:
                self._rdd = rdd
            else:
                self._rdd = self._block(rdd, block_size)
        else:
            pass  # raise exception

    def _block(self, rdd, block_size):
        return rdd.mapPartitions(lambda x: _block_collection(x, block_size))

    def __getattr__(self, attr):
        def bypass(*args, **kwargs):
            result = getattr(self._rdd, attr)(*args, **kwargs)
            if isinstance(result, RDD):
                if result is not self._rdd:
                    return self.__class__(result, False)
                else:
                    return self
            return result

        if not hasattr(self._rdd, attr):
            raise AttributeError("{0} object has no attribute {1}".format(
                                 self.__class__, attr))
        return bypass

    def __repr__(self):
        return "{0} from {1}".format(self.__class__, repr(self._rdd))

    def __getitem__(self, key):
        indexed = self._rdd.zipWithIndex()
        if isinstance(key, slice):
            indices = range(self.count())[key]
            ascending = key.step > 0
            return indexed.filter(lambda (x, i): i in indices) \
                          .sortBy(lambda (x, i): i, ascending) \
                          .map(lambda (x, i): x)
        elif isinstance(key, int):
            return indexed.filter(lambda (x, i): i == key) \
                          .first()[0]
        elif hasattr(key, "__iter__"):
            return indexed.filter(lambda (x, i): i in key) \
                          .map(lambda (x, i): x)
        else:
            raise KeyError("Unexpected type of key: {0}".format(type(key)))

    def __len__(self):
        # returns number of elements (not blocks)
        return self.shape[0]

    @property
    def partitions(self):  # numpart?
        # returns number of partitions of rdd
        return self._rdd.getNumPartitions()

    @property
    def blocks(self):
        # returns number of blocks
        return self._rdd.count()

    @property
    def shape(self):
        first = self.first().shape
        shape = self._rdd.map(lambda x: x.shape[0]).reduce(operator.add)
        return (shape,) + first[1:]

    def tolist(self):
        return self._rdd.flatMap(lambda x: list(x))

    def toiter(self):
        javaiter = self._rdd._jrdd.toLocalIterator()
        return self._rdd._collect_iterator_through_file(javaiter)

    def map(self, f, preserves_partitioning=False):
        return ArrayRDD(self._rdd.map(f, preserves_partitioning), False)

    def transform(self, f):
        return self.map(f)


class TupleRDD(ArrayRDD):

    def _block(self, rdd, block_size):
        return rdd.mapPartitions(lambda x: _block_tuple(x, block_size))

    def __len__(self):
        # returns number of elements (not blocks)
        return self.__getitem__(0).shape[0]

    def __getitem__(self, key):
        if hasattr(key, "__iter__"):
            rdd = self._rdd.map(lambda x: tuple(x[i] for i in key))
            return TupleRDD(rdd, False)
        else:
            rdd = self._rdd.map(lambda x: x[key])
            return ArrayRDD(rdd, False)

    @property
    def shape(self):
        columns = len(self.first())
        rows = self.__len__()
        return (rows, columns)

    @property
    def ix(self):
        return ArrayRDD(self)

    def transform(self, f, column=None):
        if column is not None:
            mapper = lambda x: x[:column] + (f(x[column]),) + x[column + 1:]
        else:
            mapper = f
        return TupleRDD(super(TupleRDD, self).map(mapper), block_size=False)


class DictRDD(TupleRDD):

    def __init__(self, rdd, columns, block_size=None):
        super(DictRDD, self).__init__(rdd, block_size)
        if not hasattr(columns, "__iter__"):
            raise ValueError("Columns parameter must be iterable!")
        elif not all([isinstance(k, basestring) for k in columns]):
            raise ValueError("Every column must be a string!")
        if len(columns) != len(self.first()):  # optional?
            raise ValueError("Number of values doesn't match with columns!")
        self._cols = tuple(columns)

    def __getitem__(self, key):
        if tuple(key) == self._cols:
            return self
        if hasattr(key, "__iter__"):
            indices = [self._cols.index(k) for k in key]
            return DictRDD(super(DictRDD, self).__getitem__(indices), key)
        else:
            return super(DictRDD, self).__getitem__(self._cols.index(key))

    def __contains__(self, key):
        return key in self._cols

    @property
    def columns(self):
        return self._cols

    def transform(self, f, column=None):
        if column is not None:
            column = self._cols.index(column)
        transformed = super(DictRDD, self).transform(f, column)
        return DictRDD(transformed, columns=self._cols)
