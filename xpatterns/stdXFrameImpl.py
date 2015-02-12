"""
This module provides an implementation of XFrame using pySpark RDDs.
"""
from pyspark import StorageLevel
from pyspark import SparkContext
from pyspark.sql import *

from xpatterns.commonImpl import CommonSparkContext
import xpatterns as xp

import sys
import math
import random
import inspect
import json
import random
import numpy
import array
import pickle
import errno
import shutil
import csv

class CmpRows(object):
    """ Comparison wrapper for a rwo.

    Rows can be sorted on one or more columns, and each one
    may be ascending or descending.  
    This class wraps the row, remembers the column indexes used for
    comparing the rows, and each ones ascending/descending flag.
    It provides the needed comparison functions for sorting.

    Rows are assumed to be indexable collections of values.
    Values may be any python type that itself is comparable.
    The underlying python comparison functions are used on these values.
    """
    def __init__(self, row, indexes, ascending):
        """ Instantiate a wrapped row. """
        self.row = row
        self.indexes = indexes
        self.ascending = ascending

    def less(self, other):
        """ True if self is less than other.  

        Comparison is reversed when a row is marked descending.
        """
        for index, ascending in zip(self.indexes, self.ascending):
            left = self.row[index]
            right = other.row[index]
            if left < right: return ascending
            if left > right: return not ascending
        return False

    def greater(self, other):
        """ True if self is greater than other.  

        Comparison is reversed when a row is marked descending.
        """
        for index, ascending in zip(self.indexes, self.ascending):
            left = self.row[index]
            right = other.row[index]
            if left > right: return ascending
            if left < right: return not ascending
        return False

    def equal(self, other):
        """ True when self is equal to other.

        Only comparison fields are used in this test.
        """
        for index in self.indexes:
            left = self.row[index]
            right = other.row[index]
            if left > right: return False
            if left < right: return False
        return True
    
    # These are the comparison interface
    def __lt__(self, other):
        return self.less(other)
    def __gt__(self, other):
        return self.greater(other)
    def __eq__(self, other):
        return self.equal(other)
    def __le__(self, other):
        return selfless(other) or self.equal(other)
    def __ge__(self, other):
        return self.greater(other) or self.equal(other)
    def __ne__(self, other):
        return not self.equal(other)

# Define aggregator functions for groupby.
# Each of these functions operates on a pyspark resultIterable
#  produced by groupByKey and directly produces the aggregated result.

#aggregator_functions = {}
def agg_sum(rows, cols): 
    # cols: [src_col]
    vals = [row[cols[0]] for row in rows]
    return sum(vals)

def agg_argmax(rows, cols): 
    # cols: [agg_col, out_col]
    vals = [row[cols[0]] for row in rows]
    row_index = vals.index(max(vals))
    vals = [row[cols[1]] for row in rows]
    return vals[row_index]

def agg_argmin(rows, cols): 
    # cols: [agg_col, out_col]
    vals = [row[cols[0]] for row in rows]
    row_index = vals.index(min(vals))
    vals = [row[cols[1]] for row in rows]
    return vals[row_index]

def agg_max(rows, cols): 
    # cols: [src_col]
    vals = [row[cols[0]] for row in rows]
    return max(vals)

def agg_min(rows, cols): 
    # cols: [src_col]
    vals = [row[cols[0]] for row in rows]
    return min(vals)

def agg_count(rows, cols): 
    # cols: []
    return len(rows)

def agg_avg(rows, cols): 
    # cols: [src_col]
    vals = [row[cols[0]] for row in rows]
    return numpy.mean(vals)

def agg_var(rows, cols): 
    # cols: [src_col]
    vals = [row[cols[0]] for row in rows]
    return numpy.var(vals)

def agg_std(rows, cols): 
    # cols: [src_col]
    vals = [row[cols[0]] for row in rows]
    return numpy.std(vals)

def agg_select_one(rows, cols):
    # cols: [src_col, seed]
    num_rows = len(rows)
    seed = cols[1]
    random.seed(seed)
    row_index = random.randint(0, num_rows-1)
    vals = [row[cols[0]] for row in rows]
    val = vals[row_index]
    return val

def agg_concat_list(rows, cols): 
    # cols: [src_col]
    vals = [row[cols[0]] for row in rows]
    return vals

def agg_concat_dict(rows, cols): 
    # cols: [src_col dict_value_column]
    vals = {row[cols[0]]: row[cols[1]] for row in rows}
    return vals

def agg_quantile(rows, cols): 
    # cols: [src_col, quantile]
    # cols: [src_col, [quantile ...]]
    return None


def delete_file_or_dir(path):
    expected_errs = [errno.ENOENT]     # no such file or directory
    try:
        shutil.rmtree(path)
    except OSError as err:
        if err.errno not in expected_errs:
            raise err

class AggregatorPropertySet:
    """ Store aggregator properties for one aggregator. """

    def __init__(self, name, agg_function, default_col_name, output_type):
        """ 
        Create a new instance.

        Parameters
        ----------
        name: str
            The aggregator internal name.

        agg_function: func(rows, cols)
            The agregator function.  
            This is given a pyspark resultIterable produced by groupByKey
               and containing the rows matching a single group.
            It's responsibility is to compute and return the aggregate value for thhe group.

        default_col_name: str
            The name of the aggregate column, if not supplied explicitly.
    
        output_type: type or int
            If a type is given, use that type as the output column type.
            If an integer is given, then the output type is the same as the
                input type of the column indexed by the integer.
        """

        self.name = name
        self.agg_function = agg_function
        self.default_col_name = default_col_name
        self.output_type = output_type

    def get_output_type(self, input_type):
        candidate = self.output_type
        if type(candidate) is int: return input_type[candidate]
        return candidate

class AggregatorProperties:
    """ Manage aggregator properties for all known aggregators. """
    def __init__(self):
        self.aggregator_properties = {}

    def add(self, aggregator_property_set):
        self.aggregator_properties[aggregator_property_set.name] = aggregator_property_set


    def __getitem__(self, op):
        if not op in self.aggregator_properties:
            raise ValueError('unrecognized aggregation operator: {}'.format(op))
        return self.aggregator_properties[op]

aggregator_properties = AggregatorProperties()

aggregator_properties.add(AggregatorPropertySet('__builtin__sum__', agg_sum, 'sum', int))
aggregator_properties.add(AggregatorPropertySet('__builtin__argmax__', agg_argmax, 'argmax', 1))
aggregator_properties.add(AggregatorPropertySet('__builtin__argmin__', agg_argmin, 'argmin', 1))
aggregator_properties.add(AggregatorPropertySet('__builtin__max__', agg_max, 'max', 0))
aggregator_properties.add(AggregatorPropertySet('__builtin__min__', agg_min, 'min', 0))
aggregator_properties.add(AggregatorPropertySet('__builtin__count__', agg_count, 'count', int))
aggregator_properties.add(AggregatorPropertySet('__builtin__avg__', agg_avg, 'avg', float))
aggregator_properties.add(AggregatorPropertySet('__builtin__mean__', agg_avg, 'mean', float))
aggregator_properties.add(AggregatorPropertySet('__builtin__var__', agg_var, 'var', float))
aggregator_properties.add(AggregatorPropertySet('__builtin__variance__', agg_var, 'variance', float))
aggregator_properties.add(AggregatorPropertySet('__builtin__std__', agg_std, 'std', float))
aggregator_properties.add(AggregatorPropertySet('__builtin__stdv__', agg_std, 'stdv', float))
aggregator_properties.add(AggregatorPropertySet('__builtin__select_one__', agg_select_one, 'select_one', 0))
aggregator_properties.add(AggregatorPropertySet('__builtin__concat__list__', agg_concat_list, 'concat', list))
aggregator_properties.add(AggregatorPropertySet('__builtin__concat__dict__', agg_concat_dict, 'concat', dict))
aggregator_properties.add(AggregatorPropertySet('__builtin__quantile__', agg_quantile, 'quantile', float))



# Helper functions

def is_missing(x):
    """ Tests for missing values. """
    if x is None: return True
    if isinstance(x, float) and math.isnan(x): return True
    return False

def is_missing_or_empty(val):
    """ Tests for missing or empty values. """
    if is_missing(val): return True
    if type(val) in (list, dict):
        if len(val) == 0: return True
    return False

def name_col(existing_col_names, proposed_name):
    """ Give a column a unique name.

    If the name already exists, create a unique name
    by appending a number.
    """
    # if there is a dup, add .<n> to make name unique
    candidate = proposed_name
    i = 1
    while candidate in existing_col_names:
        candidate = '{}.{}'.format(proposed_name, i)
        i += 1
    return candidate

# TODO make a static function and move this to a base class
# Safe version of zip.
# This requires that left and right RDDs be of the same length, but
#  not the same partition structure
def safeZip(left, right):
    ix_left = left.zipWithIndex().map(lambda row: (row[1], row[0]))
    ix_right = right.zipWithIndex().map(lambda row: (row[1], row[0]))
    return ix_left.join(ix_right).values()

class StdXFrameImpl:
    """ Implementation for XFrame. """

    def __init__(self, rdd=None, col_names=None, column_types=None, trace=False):
        """ Instantiate a XFrame implementation.

        The RDD holds all the data for the XFrame.
        The rows in the rdd are stored as a list.
        Each column must be of uniform type.
        Types permitted include int, long, float, string, list, and dict.
        """
        col_names = col_names or []
        column_types = column_types or []
        self.rdd = rdd
        self.col_names = list(col_names)
        self.column_types = list(column_types)

        self.materialized = False
        self.entry_trace = False
        self.exit_trace = False

    def _rv(self, rdd, col_names=None, column_types=None):
        """
        Return a new StdXFrameImpl containing the RDD, column names, and column types.

        Column names and types default to the existing ones.
        This is typically used when a function returns a new XFrame.
        """
        # only use defaults if values are None, not []
        col_names = self.col_names if col_names is None else col_names
        column_types = self.column_types if column_types is None else column_types
        return StdXFrameImpl(rdd, col_names, column_types)

    def _reset():
        self.rdd = None
        self.col_names = []
        self.column_types = []

        self.materialized = False

    def _persist(self):
        self.rdd.persist(StorageLevel.MEMORY_ONLY)

    def _replace(self, rdd, col_names=None, column_types=None):
        """
        Replaces the existing RDD, column names, and column types with new values.

        Column names and types default to the existing ones.
        This is typically used when a function modifies the current XFrame.
        """
        self.rdd = rdd
        if col_names is not None: self.col_names = col_names
        if column_types is not None: self.column_types = column_types
        self.materialized = False
        return self

    def _count(self):
#        self._persist()
        count = self.rdd.count()     # action
        self.materialized = True
        return count

    def _entry(self, *args):
        """ Trace function entry. """
        if self.entry_trace:
            print 'enter xFrame', inspect.stack()[1][3], args

    def _exit(self, *args):
        """ Trace function exit. """
        if self.exit_trace:
            print 'exit xFrame', inspect.stack()[1][3], args

    # Load
    def load_from_dataframe(self, data):
        """
        Load RDD from a pandas DataFrame.
        """
        self._entry(data)
        self._exit()
        raise NotImplementedError()

    def load_from_xframe_index(self, path):
        self._entry(path)
        metadata_path = path + '.metadata'
        with open(metadata_path) as f:
            names, types = pickle.load(f)
        sc = CommonSparkContext.Instance().sc
        res = sc.pickleFile(path)
        self._replace(res, names, types)
        self._exit()        

    def load_from_csv(self, path, parsing_config, type_hints):
        """
        Load RDD from a csv file
        """
        self._entry(path, parsing_config, type_hints)

#        print 'parsing_config', parsing_config
#        print 'type_hints', type_hints

        def get_config(name):
            return parsing_config[name] if name in parsing_config else None
        row_limit = get_config('row_limit')
        use_header = get_config('use_header')
        comment_char = get_config('comment_char')
        na_values = get_config('na_values')
        if not type(na_values) == list:
            na_values = [na_values]

        sc = CommonSparkContext.Instance().sc
        raw = sc.textFile(path)
        #parsing_config 
        # 'row_limit': 100, 
        # 'use_header': True, 

        # 'double_quote': True, 
        # 'skip_initial_space': True, 
        # 'delimiter': '\n', 
        # 'quote_char': '"', 
        # 'escape_char': '\\'

        # 'comment_char': '', 
        # 'na_values': ['NA'], 
        # 'continue_on_failure': True, 
        # 'store_errors': False, 

        def apply_comment(line, comment_char):
            return line.partition(comment_char)[0].rstrip()
        if comment_char:
            raw = raw.map(lambda line: apply_comment(line, comment_char), preservesPartitioning=True)

        def to_format_params(config):
            params = {}
            parm_map = {
                # parse_config: read_csv
                'delimiter': 'delimiter',
                'doublequote': 'doublequote',
                'escape_char': 'escapechar',
                'quote_char': 'quotechar',
                'skip_initial_space': 'skipinitialspace'
            }
            for pc, rc in parm_map.iteritems():
                if pc in config: params[rc] = config[pc]
            return params
        
        params = to_format_params(parsing_config)
        if row_limit:
            if row_limit > 100:
                pairs = raw.zipWithIndex()
                pairs.persist(StorageLevel.MEMORY_ONLY)
                filtered_pairs = pairs.filter(lambda x: x[1] < row_limit)
                pairs.unpersist()
                raw = filtered_pairs.keys()
            else:
                lines = raw.take(row_limit)
                raw = sc.parallelize(lines)
        # TODO extremely inefficient to create a reader for each line
        # Use per partition operations to create a reader once per partition
        # See p 106: Learning Spark
        # See mapPartitions
        def csv_to_array(line, params):
            reader = csv.reader([line], **params)
            return reader.next()
        res = raw.map(lambda row: csv_to_array(row, params), preservesPartitioning=True)
         
        # use first row, if available, to make column names
        first = res.first()
        if use_header:
            col_names = first
            # take off first line
            # TODO there must be a better way to strip one line
            res = res.zipWithUniqueId()
            res = res.filter(lambda kv: kv[1] != 0)
            res = res.keys()
        else:
            col_names = ['X.{}'.format(i) for i in range(len(first))]

        # Transform hints: __X{}__ ==> name.
        # If it is not of this form, leave it alone.
        def extract_index(s):
            if s.startswith('__X') and s.endswith('__'):
                index = s[3:-2]
                return int(index)
            return None
        def map_col(col, col_names):
            # Change key on hints from generated names __X<n>__
            #   into the actual column name
            index =  extract_index(col)
            if index is None:
                return col
            return col_names[index]
            
        # get desired column types
        if '__all_columns__' in type_hints:
            # all cols are of the given type
            typ = type_hints['__all_columns__']
            types = [typ for i in first]
        else:
            # all cols are str, except the one(s) mentioned
            types = [str for i in first]
            # change generated hint key to actual column name
            type_hints =  {map_col(col, self.col_names): typ for col, typ in type_hints.iteritems()}
            for col in self.col_names:
                if col in type_hints:
                    types[self.col_names.index(col)] = type_hints[col]
        column_types = types
        
        # apply na values
        def apply_na(row, na_values):
            return [None if val in na_values else val for val in row]
        res = res.map(lambda row: apply_na(row, na_values), preservesPartitioning=True)

        # cast to desired type
        def cast_val(val, typ):
            return None if val is None else typ(val)
        def cast_row(row, types):
#            return [typ(val) for val, typ in zip(row, types)]
            return [cast_val(val, typ) for val, typ in zip(row, types)]

        res = res.map(lambda row: cast_row(row, types), preservesPartitioning=True)
        self._replace(res, col_names, column_types)

        self._exit()
        # returns a dict of errors
        return {}

    # Save
    def save(self, path):
        """
        Save to a file.  

        Saved in an efficient internal format, intended for reading back into an RDD.
        """
        self._entry(path)
        metadata_path = path + '.metadata'
        metadata = [self.col_names, self.column_types]
        with open(metadata_path, 'w') as f:
            pickle.dump(metadata, f)
        delete_file_or_dir(path)
        self.rdd.saveAsPickleFile(path)        # action ?
        self._exit()

    def save_as_csv(self, url, **args):
        """
        Save to a text file in csv format.
        """
        self._entry(url, **args)
        # TODO save col names and types
        self._exit()
        raise NotImplementedError()


    def to_schema_rdd(self, number_of_partitions):
        """
        Adds column name and type informaton to the rdd and returns it.
        """
        def translateType(typ):
            if typ == str: return StringType()
            if typ == bool: return BooleanType()
            if typ == float: return FloatType()
            if typ == int or typ == long: return IntegerType()
            if typ == list: return ArrayType()
            if typ == dict: return MapType()
            return StringType()
        self._entry(number_of_partitions)
        fields = [StructField(name, translateType(typ), True) for name, typ in zip(self.col_names, self.column_types)]
        schema = StructType(fields)
        rdd = self.rdd.repartition(number_of_partitions)
        sqlc = CommonSparkContext.Instance().sqlc
        res = sqlc.applySchema(rdd, schema)
        name = 'abc'
        res.registerTempTable(name)
        self._exit()
        return res

    def to_rdd(self, number_of_partitions):
        """
        Returns the internal rdd.
        """
        self._entry(number_of_partitions)
        res = self.rdd.repartition(number_of_partitions)
        self._exit()
        return res

    # Table Information
    def num_rows(self):
        """
        Returns the number of rows of the RDD.
        """
        # TODO: this forces the RDD to be computed.
        # When it is used again, it must be recomputed.
        self._entry()
        if self.rdd is None: return 0
        count = self._count()      # action
        self._exit(count)
        return count

    def num_columns(self):
        """
        Returns the number of columns in the XFrame.
        """
        self._entry()
        res = len(self.col_names)
        self._exit(res)
        return res

    def column_names(self):
        """
        Returns the column names in the XFrame.
        """
        self._entry()
        res = self.col_names
        self._exit(res)
        return res

    def dtype(self):
        """
        Returns the column data types in the XFrame.
        """
        self._entry()
        res = self.column_types
        self._exit(res)
        return res

    # Get Data
    def head(self, n):
        """
        Return the first n rows of the RDD as an XFrame.
        """
        # Returns an XFrame, otherwise we would use take(n)
        # TODO: this is really inefficient: it numbers the whole thing, and
        #  then filters most of it out.
        # Maybe it would be better to use take(n) then parallelize ?
        self._entry(n)
#        print self.rdd.toDebugString()
        if n <= 100:
            data = self.rdd.take(n)
            sc = CommonSparkContext.Instance().sc
            res = sc.parallelize(data)
            self._exit(res)
            return self._rv(res)
        self._persist()
        pairs = self.rdd.zipWithIndex()
        pairs.persist(StorageLevel.MEMORY_ONLY)
        filtered_pairs = pairs.filter(lambda x: x[1] < n)
        pairs.unpersist()
        res = filtered_pairs.keys()
        self._exit(res)
        # test
        res.persist(StorageLevel.MEMORY_ONLY)
        self.materialized = True
        return self._rv(res)

    def head_as_list(self, n):
        # Used in xframe when doing dry runs to determine type
        self._entry(n)
        self._persist()
        lst = self.rdd.take(n)      # action
        self._exit(lst)
        return lst

    def tail(self, n):
        """
        Return the last n rows of the RDD as an XFrame.
        """
        self._entry(n)
        pairs = self.rdd.zipWithIndex()
        pairs.persist(StorageLevel.MEMORY_ONLY)
        start = pairs.count() - n
        filtered_pairs = pairs.filter(lambda x: x[1] >= start)
        pairs.unpersist()
        res = filtered_pairs.map(lambda x: x[0], preservesPartitioning=True)
        self._exit(res)
        return self._rv(res)

    # Sampling
    def sample(self, fraction, seed):
        """
        Sample the current RDDs rows as an XFrame.
        """
        self._entry(fraction, seed)
        res = self.rdd.sample(False, fraction, seed)
        self._exit(res)
        return self._rv(res)

    def random_split(self, fraction, seed):
        """
        Randomly split the rows of an XFrame into two XFrames. The first XFrame
        contains *M* rows, sampled uniformly (without replacement) from the
        original XFrame. *M* is approximately the fraction times the original
        number of rows. The second XFrameD contains the remaining rows of the
        original XFrame.
        """
        # There is random split in the scala RDD interface, but not in pySpark.
        # Assign random number to each row and filter the two sets.
        self._entry(fraction, seed)
        seed = seed if seed is not None else random.randint(0, sys.maxint)
        rng = random.Random(seed)
        rdd = self.rdd
        self._persist()
        rand_col = self.rdd.map(lambda row: rng.uniform(0.0, 1.0), preservesPartitioning=True)
        # zip restrictions is met
        labeled_rdd = self.rdd.zip(rand_col)
        labeled_rdd.persist(StorageLevel.MEMORY_ONLY)
        rdd1 = labeled_rdd.filter(lambda row: row[1] < fraction).map(lambda row: row[0], preservesPartitioning=True)
        rdd2 = labeled_rdd.filter(lambda row: row[1] >= fraction).map(lambda row: row[0], preservesPartitioning=True)
        labeled_rdd.unpersist()
        self._exit(rdd1, rdd2)
        return self._rv(rdd1), self._rv(rdd2)

    # Materialization
    def materialize(self):
        """
        For an RDD that is lazily evaluated, force the persistence of the
        RDD, committing all lazy evaluated operations.
        """
        self._entry()
        self._count()
        self._exit()

    def is_materialized(self):
        """
        Returns whether or not the RDD has been materialized.
        """
        self._entry()
        res = self.materialized
        self._exit(res)
        return res

    def has_size(self):
        """
        Returns whether or not the size of the XFrame is known.
        """
        self._entry()
        res = self.materialized
        self._exit(res)
        return res

    # Column Manipulation
    def select_column(self, column_name):
        """
        Get the array RDD that corresponds with
        the given column_name as an XArray.
        """
        self._entry(column_name)
        col = self.col_names.index(column_name)
        res = self.rdd.map(lambda row: row[col], preservesPartitioning=True)
        col_type = self.column_types[col]
        self._exit(res, col_type)
        return xp.stdXArrayImpl.StdXArrayImpl(res, col_type)

    def select_columns(self, keylist):
        """
        Creates RDD composed only of the columns referred to in the given list of
        keys, as an XFrame.
        """
        self._entry(keylist)
        def get_columns(row, cols):
            return [row[col] for col in cols]
        cols = [self.col_names.index(key) for key in keylist]
        names = [self.col_names[col] for col in cols]
        types = [self.column_types[col] for col in cols]
        res = self.rdd.map(lambda row: get_columns(row, cols), preservesPartitioning=True)
        self._exit(res, names, types)
        return self._rv(res, names, types)

    def add_column(self, data, name):
        """
        Add a column to this XFrame. The number of elements in the data given
        must match the length of every other column of the XFrame. This
        operation modifies the current XFrame in place and returns self. If no
        name is given, a default name is chosen.
        """
        self._entry(data, name)        
        col = len(self.col_names)
        if name == '':
            name = 'X{}'.format(col)
        if name in self.col_names:
            raise ValueError('column name already exists: {}'.format(name))
        self.col_names.append(name)
        self.column_types.append(data.elem_type)
        # zip the data into the rdd, then shift into the list
        if self.rdd is None:
            res = data.rdd.map(lambda x: [x], preservesPartitioning=True)
        else:
            res = safeZip(self.rdd, data.rdd)
            def move_inside(old_val, new_elem):
                return old_val + [new_elem]
            res = res.map(lambda pair: move_inside(pair[0], pair[1]), preservesPartitioning=True)
        self._exit(res)
        return self._replace(res)

    def add_columns_array(self, cols, namelist):
        """
        Adds multiple columns to this XFrame. The number of elements in all
        columns must match the length of every other column of the RDDs. 
        Each column added is an XArray.
        This operation modifies the current XFrame in place and returns self.
        """
        self._entry(cols, namelist)
        names = self.col_names + namelist
        types = self.column_types + [col.__impl__.elem_type for col in cols]
        rdd = self.rdd
        for col in cols:
            rdd = safeZip(rdd, col.__impl__.rdd)
            def move_inside(old_val, new_elem):
                return old_val + [new_elem]
            rdd = rdd.map(lambda pair: move_inside(pair[0], pair[1]), preservesPartitioning=True)
        self._exit(rdd, names, types)
        return self._replace(rdd, names, types)

    def add_columns_frame(self, other):
        """
        Adds multiple columns to this XFrame. The number of elements in all
        columns must match the length of every other column of the RDD. 
        The columns to be added are in an XFrame.
        This operation modifies the current XFrame in place and returns self.
        """
        self._entry(other)
        names = self.col_names + other.__impl__.col_names
        types = self.column_types + other.__impl__.column_types
        def merge(old_cols, new_cols):
            return old_cols + new_cols
        # zip restriction: data must match in length and partition structure
        rdd = safeZip(self.rdd, other.__impl__.rdd)
        res = rdd.map(lambda pair: merge(pair[0], pair[1]), preservesPartitioning=True)
        self._exit(res, names, types)
        return self._replace(res, names, types)

    def remove_column(self, name):
        """
        Remove a column from the RDD. 

        This operation modifies the current XFrame in place and returns self.
        """
        self._entry(name)
        col = self.col_names.index(name)
        self.col_names.pop(col)
        self.column_types.pop(col)
        def pop_col(row, col):
            row.pop(col)
            return row
        res = self.rdd.map(lambda row: pop_col(row, col), preservesPartitioning=True)
        self._exit(res)
        return self._replace(res)

    def remove_columns(self, col_names):
        """
        Remove columns from the RDD. 

        This operation modifies the current XFrame in place and returns self.
        """
        self._entry(col_names)
        cols = [self.col_names.index(name) for name in col_names]
        # pop from highets to lowest does not foul up indexes
        cols.sort(reverse=True)
        for col in cols:
            self.col_names.pop(col)
            self.column_types.pop(col)
        def pop_cols(row, cols):
            for col in cols:
                row.pop(col)
            return row
        res = self.rdd.map(lambda row: pop_cols(row, cols), preservesPartitioning=True)
        self._exit(res)
        return self._replace(res)

    def swap_columns(self, column_1, column_2):
        """
        Swap columns of the RDD.

        This operation modifies the current XFrame in place and returns self.
        """
        self._entry(column_1, column_2)
        def swap_list(lst, col1, col2):
            new_list = list(lst)
            new_list[col1] = lst[col2]
            new_list[col2] = lst[col1]
            return new_list
        def swap_cols(row, col1, col2):
            # is it OK to modify the row ?
            tmp = row[col1]
            row[col1] = row[col2]
            row[col2] = tmp
            return row
        col1 = self.col_names.index(column_1)
        col2 = self.col_names.index(column_2)
        names = swap_list(self.col_names, col1, col2)
#        names = list(self.col_names)
#        names[col2] = self.col_names[col1]
#        names[col1] = self.col_names[col2]
        types = swap_list(self.column_types, col1, col2)
#        types = list(self.column_types)
#        types[col2] = self.column_types[col1]
#        types[col1] = self.column_types[col2]
        res = self.rdd.map(lambda row: swap_cols(row, col1, col2), preservesPartitioning=True)
        self._exit(res, names, types)
        return self._replace(res, names, types)

    def set_column_name(self, old_name, new_name):
        """
        Rename the given column.

        No return value.
        """
        self._entry(old_name, new_name)
        col = self.col_names.index(old_name)
        self.col_names[col] = new_name
        self._exit()

    # Iteration

    # Begin_iterator is called by a generator function, local to __iter__.
        # It calls iterator_get_next to fetch a group of items, then returns them one by one
        # using yield.  It keeps calling iterator_get_next as long as there are elements 
        # remaining.  It seems like only one iterator at a time can be operating because
        # the position is stored here.  Would it be better to let the caller handle the iter_pos?
        #
        # This uses zipWithIndex, which needs to process the whole data set.  
        # Is it better to use take or collect ?  OR are they effectively the same since zipWithIndex 
        # has just run?
    def begin_iterator(self):
        # TODO: be sure to reset this when the RDD changes.
        self._entry()
        self._exit()
        self.iter_pos = -1

    def iterator_get_next(self, elems_at_a_time):
        self._entry(elems_at_a_time)
        low = self.iter_pos
        high = self.iter_pos + elems_at_a_time
        buf_rdd = self.rdd.zipWithIndex()
        filtered_rdd = buf_rdd.filter(lambda row: row[1] >= low and row[1] < high)
        trimmed_rdd = filtered_rdd.map(lambda row: row[0], preservesPartitioning=True)
        iter_buf = trimmed_rdd.collect()
        self.iter_pos += elems_at_a_time
        self._exit(iter_buf)
        return iter_buf

    def replace_single_column(self, col):
        """
        Replace the column in a single-column table with the given one.

        This operation modifies the current XFrame in place and returns self.
        """
        self._entry(col)
        res = col.__impl__.rdd.map(lambda item: [item], preservesPartitioning=True)
        self._exit(res)
        return self._replace(res)

    # Row Manipulation
    def flat_map(self, fn, column_names, column_types, seed):
        """
        Map each row of the RDD to multiple rows in a new RDD via a
        function.

        The input to `fn` is a dictionary of column/value pairs.
        The output of `fn` must have type List[List[...]].  Each inner list
        will be a single row in the new output, and the collection of these
        rows within the outer list make up the data for the output RDD.
        """
        self._entry(fn, column_names, column_types, seed)
        names = self.col_names
        # fn needs the row as a dict
        res = self.rdd.flatMap(lambda row: fn(dict(zip(names, row))), preservesPartitioning=True)
        self._exit(res, column_names, column_types)
        return self._rv(res, column_names, column_types)

    def logical_filter(self, other):
        """
        Where other is an array RDD of identical length as the current one,
        this returns a selection of a subset of rows in the current RDD
        where the corresponding row in the selector is non-zero.
        """
        self._entry(other)
        # zip restriction: data must match in length and partition structure

        pairs = safeZip(self.rdd, other.rdd)

        res = pairs.filter(lambda p: p[1]).map(lambda p: p[0], preservesPartitioning=True)
#        print 'logical_filter after filter'
#        print res.toDebugString()
        self._exit(res)
        return self._rv(res)

    def stack_list(self, column_name, new_column_names, new_column_types, drop_na):
        """
        Convert a "wide" list column of an XFrame to one or two "tall" columns by
        stacking all values.
        
        new_column_names and new_column_types are lists of 1 or 2 items
        """
        self._entry(column_name, new_column_names, new_column_types, drop_na)
        col_num = self.col_names.index(column_name)
        def subs_row(row, col, val):
            new_row = list(row)
            new_row[col] = val
            return new_row
        def stack_row(row, col, drop_na):
            res = []
            for val in row[col]:
                if drop_na and is_missing_or_empty(val): continue
                res.append(subs_row(row, col, val))
            if len(res) > 0 or drop_na:
                return res
            return [subs_row(row, col, None)]
        res = self.rdd.flatMap(lambda row: stack_row(row, col_num, drop_na), preservesPartitioning=True)

        column_names = list(self.col_names)
        new_name = new_column_names[0]
        if new_name == '':
            new_name = name_col(column_names, 'X')
        column_names[col_num] = new_name
        column_types = list(self.column_types)
        column_types[col_num] = new_column_types[0]
        self._exit(res, column_names, column_types)
        return self._rv(res, column_names, column_types)

    def stack_dict(self, column_name, new_column_names, new_column_types, drop_na):
        """
        Convert a "wide" dict column of an XFrame to two "tall" columns by
        stacking all values.
        
        new_column_names and new_column_types are lists of 2 items
        """
        self._entry(column_name, new_column_names, new_column_types, drop_na)
        col_num = self.col_names.index(column_name)
        def subs_row(row, col, key, val):
            new_row = list(row)
            new_row[col] = key
            new_row.insert(col+1, val)
            return new_row
        def stack_row(row, col, drop_na):
            res = []
            for key, val in row[col].iteritems():
                if drop_na and is_missing_or_empty(val): continue
                res.append(subs_row(row, col, key, val))
            if len(res) > 0 or drop_na:
                return res
            return [subs_row(row, col, None, None)]
        res = self.rdd.flatMap(lambda row: stack_row(row, col_num, drop_na))

        column_names = list(self.col_names)
        new_name_k = new_column_names[0]
        if new_name_k == '':
            new_name_k = name_col(column_names, 'K')
        column_names[col_num] = new_name_k
        new_name_v = new_column_names[1]
        if new_name_v == '':
            new_name_v = name_col(column_names, 'V')
        column_names.insert(col_num + 1, new_name_v)
        column_types = list(self.column_types)
        column_types[col_num] = new_column_types[0]
        column_types.insert(col_num + 1, new_column_types[1])
        self._exit(res, column_names, column_types)
        return self._rv(res, column_names, column_types)

    def append(self, other):
        """
        Add the rows of an RDD to the end of this RDD.

        Both RDDs must have the same set of columns with the same column
        names and column types.
        """
        self._entry(other)
        res = self.rdd.union(other.rdd)
        self._exit(res)
        return self._rv(res)

    def copy_range(self, start, step, stop):
        """
        Returns an RDD consisting of the values between start and stop, counting by step.
        """
        self._entry(start, step, stop)
        def select_row(x, start, step, stop):
            if x < start or x >= stop: return False
            return (x - start) % step == 0
        pairs = self.rdd.zipWithIndex()
        res = pairs.filter(lambda x: select_row(x[1], start, step, stop)).map(lambda x: x[0], preservesPartitioning=True)
        self._exit(res)
        return self._rv(res)

    def drop_missing_values(self, columns, all_behavior, split):
        """
        Remove missing values from an RDD. A missing value is either ``None``
        or ``NaN``.  If ``all_behavior`` is False, a row will be removed if any of the
        columns in the ``columns`` parameter contains at least one missing
        value.  If ``all_behavior`` is True, a row will be removed if all of the columns
        in the ``columns`` parameter are missing values.

        If the ``columns`` parameter is the empty list, 
        consider all columns when searching for missing values.
        """
        self._entry(columns, all_behavior, split)
        def keep_row_all(row, cols):
            for col in cols:
                if not is_missing(row[col]): return True
            return False
        def keep_row_any(row, cols):
            for col in cols:
                if is_missing(row[col]): return False
            return True

        column_names = self.col_names if len(columns) == 0 else columns
        cols = [self.col_names.index(col) for col in column_names]
        f = keep_row_all if all_behavior else keep_row_any
        if not split:
            res = self.rdd.filter(lambda row: f(row, cols))
            self._exit(res)
            return self._rv(res)
        else:
            res1 = self.rdd.filter(lambda row: f(row, cols))
            res2 = self.rdd.filter(lambda row: not f(row, cols))
            self._exit(res1, res2)
            return self._rv(res1), self._rv(res2)

    def add_row_number(self, column_name, start):
        """
        Returns a new RDD with a new column that numbers each row
        sequentially. By default the count starts at 0, but this can be changed
        to a positive or negative number.  The new column will be named with
        the given column name.  
        Make sure the row number is the first column.
        """
        self._entry(column_name, start)
        def pull_up(pair, start):
            row = list(pair[0])
            row.insert(0, pair[1] + start)
            return row
        col = len(self.col_names)
        names = list(self.col_names)
        names.insert(0, column_name)
        types = list(self.column_types)
        types.insert(0, int)
        res = self.rdd.zipWithIndex().map(lambda row: pull_up(row, start), preservesPartitioning=True)
        self._exit(res, names, types)
        return self._rv(res, names, types)

    # Data Transformations Within Columns
    def pack_columns(self, columns, dict_keys, dtype, fill_na):
        """
        Pack two or more columns of the current XFrame into one single
        column.The result is a new XFrame with the unaffected columns from the
        original XFrame plus the newly created column.

        The list of columns that are packed is chosen through either the
        ``columns`` or ``column_prefix`` parameter. Only one of the parameters
        is allowed to be provided. ``columns`` explicitly specifies the list of
        columns to pack, while ``column_prefix`` specifies that all columns that
        have the given prefix are to be packed.

        The type of the resulting column is decided by the ``dtype`` parameter.
        Allowed values for ``dtype`` are dict, array.array and list:

         - *dict*: pack to a dictionary XArray where column name becomes
           dictionary key and column value becomes dictionary value

         - *array.array*: pack all values from the packing columns into an array

         - *list*: pack all values from the packing columns into a list.
        """
        self._entry(columns, dict_keys, dtype, fill_na)
        cols = [self.col_names.index(col) for col in columns]
        keys = self.rdd.map(lambda row: [row[col] for col in cols], preservesPartitioning=True)

        def substitute_missing(v, fill_na):
            return fill_na if is_missing(v) and fill_na else v
        def pack_row_list(row, fill_na):
            return [substitute_missing(v, fill_na) for v in row]
        def pack_row_array(row, fill_na, typecode):
            lst = [substitute_missing(v, fill_na) for v in row]
            return array.array(typecode, lst)
        def pack_row_dict(row, dict_keys, fill_na):
            d = {}
            for val, key in zip(row, dict_keys):
                new_val = substitute_missing(val, fill_na)
                if new_val is not None: d[key] = new_val
            return d

        if dtype == list:
            res = keys.map(lambda row: pack_row_list(row, fill_na), preservesPartitioning=True)
        elif dtype == array.array:
            typecode = 'd'
            res = keys.map(lambda row: pack_row_array(row, fill_na, typecode), preservesPartitioning=True)
        elif dtype == dict:
            res = keys.map(lambda row: pack_row_dict(row, dict_keys, fill_na), preservesPartitioning=True)
        else:
            raise NotImplementedError
        self._exit(res, dtype)
        return xp.stdXArrayImpl.StdXArrayImpl(res, dtype)

    def transform(self, fn, dtype, seed):
        """
        Transform each row to an XArray according to a
        specified function. Returns a array RDD of ``dtype`` where each element
        in this array RDD is transformed by `fn(x)` where `x` is a single row in
        the xframe represented as a dictionary.  The ``fn`` should return
        exactly one value which can be cast into type ``dtype``. 
        """
        self._entry(fn, dtype, seed)
        names = self.col_names
        # fn needs the row as a dict
        res = self.rdd.map(lambda row: dtype(fn(dict(zip(names, row)))), preservesPartitioning=True)
        self._exit(res, dtype)
        return xp.stdXArrayImpl.StdXArrayImpl(res, dtype)

    # Group, Join, and Sort
    def groupby_aggregate(self, key_columns_array, group_columns, group_output_columns, group_ops):
        """
        Perform a group on the key_columns followed by aggregations on the
        columns listed in operations.

        group_columns, group_output_columns and group_ops are all arrays of equal length
        """
        self._entry(key_columns_array, group_columns, group_output_columns, group_ops)
#        print 'key_columns_array', key_columns_array
#        print 'group_columns', group_columns
#        print 'group_ops', group_ops

        # make key column indexes
        key_cols = [self.col_names.index(col) for col in key_columns_array]

        # make group column indexes
        group_cols = [[self.col_names.index(col) if col != '' else None for col in cols] for cols in group_columns]
#        print 'group_cols', group_cols
        
        # look up operators
        # make new column names
        default_group_output_columns = [aggregator_properties[op].default_col_name for op in group_ops]
        group_output_columns = [col if col != '' else deflt 
                                    for col, deflt in zip(group_output_columns, default_group_output_columns)]
#        print 'group_output_columns', group_output_columns
        new_col_names = key_columns_array + group_output_columns
#        print 'new_col_names', new_col_names
        # make sure col names are unique
        unique_col_names = []
        for col_name in new_col_names:
            unique_name = name_col(unique_col_names, col_name)
            unique_col_names.append(unique_name)
#        print 'unique_col_names', unique_col_names
        new_col_names = unique_col_names

        def get_group_types(cols):
            return [self.column_types[col] if type(col) is int else None for col in cols]

        # make new column types
        new_col_types = [self.column_types[index] for index in key_cols]
        # get existing types of group columns
        group_types = [get_group_types(cols) for cols in group_cols]
#        print 'group_types', group_types
        agg_types = [aggregator_properties[op].get_output_type(group_type) 
                         for op, group_type in zip(group_ops, group_types)]

        new_col_types.extend(agg_types)
#        print 'new_col_types', new_col_types

        # make RDD into K,V pairs where key incorporates the key column values
        def make_key(row, key_cols):
            key = [row[col] for col in key_cols]
            return json.dumps(key)
        keyed_rdd = self.rdd.map(lambda row: (make_key(row, key_cols), row), preservesPartitioning=True)
#        print 'keyed_rdd', keyed_rdd.collect()

        grouped = keyed_rdd.groupByKey()
        grouped = grouped.map(lambda pair: (json.loads(pair[0]), pair[1]), preservesPartitioning=True)
        # (key, [row ...]) ...
#        print 'grouped', grouped.collect()
#        print 'flattened', grouped.map(lambda (x, y): (x, list(y))).collect()
        # run the aggregator on y: count --> len(y); sum --> sum(y), etc

        def build_aggregates(rows, aggregators, group_cols):
            # apply each of the aggregator functions and collect their results into a list
            return [aggregator(rows, cols) 
                     for aggregator, cols in zip(aggregators, group_cols)]
        aggregators = [aggregator_properties[op].agg_function for op in group_ops]
        aggregates = grouped.map(lambda (x, y): (x, build_aggregates(y, aggregators, group_cols)), preservesPartitioning=True)
#        print 'aggregates', aggregates.collect()
        def concatenate(old_vals, new_vals):
            return old_vals + new_vals
        
        res = aggregates.map(lambda pair: concatenate(pair[0], pair[1]), preservesPartitioning=True)
        self._exit(res, new_col_names, new_col_types)
        return self._rv(res, new_col_names, new_col_types)

    def join(self, right, how, join_keys):
        """
        Merge two XFrames. Merges the current (left) XFrame with the given
        (right) XFrame using a SQL-style equi-join operation by columns.

        join_keys is a dict of left-right column names
        how = [left, right, outer, inner]
        """
        self._entry(right, how, join_keys)
        # new columns are made up of:
        # 1) left columns
        # 2) right columns exculding join_keys.values()
        # Duplicate remaining right columns need to be renamed

        # make lists of left and right key indexes
        # these are the positions of the key columns in left and right
        # put the pieces together
        # one of the pairs may be None in all cases except inner
        def combine_results(left_row, right_row, left_count, right_count):
            if left_row is None:
                left_row = [None] * left_count
            if right_row is None:
                right_row = [None] * right_count
#            print 'left', left_row, 'right', right_row
            return left_row + right_row

        if how == 'outer':
            # outer join is substantially different
            # so do it separately
            new_col_names = list(self.col_names)
            new_col_types = list(self.column_types)
            for col in right.col_names:
                new_col_names.append(name_col(new_col_names, col))
            for t in right.column_types:
                new_col_types.append(t)
            left_count = len(self.col_names)
            right_count = len(right.col_names)
            pairs = self.rdd.cartesian(right.rdd)
        else:
            # inner, left, and right
            left_key_indexes = []
            right_key_indexes = []
            for left_key, right_key in join_keys.iteritems():
                left_index = self.col_names.index(left_key)
                left_key_indexes.append(left_index)
                right_index = right.col_names.index(right_key)
                right_key_indexes.append(right_index)
                right_key_indexes.sort(reverse=True)

            # make a list of the right column names and types
            right_col_names = list(right.col_names)
            right_col_types = list(right.column_types)
            for i in right_key_indexes:
                right_col_names.pop(i)
                right_col_types.pop(i)
        
            # make a list of the result column names and types
            # rename duplicate names
            new_col_names = list(self.col_names)
            new_col_types = list(self.column_types)
            for col in right_col_names:
                new_col_names.append(name_col(new_col_names, col))
            for t in right_col_types:
                new_col_types.append(t)
            left_count = len(self.col_names)
            right_count = len(right_col_names)

            # build a key from the column values
            # spark cannot handle tuples as keys, so make it a string
            def build_key(row, indexes):
                key = [row[i] for i in indexes]
                return json.dumps(key)

            # add keys to left and right
            keyed_left = self.rdd.map(lambda row: (build_key(row, left_key_indexes), row), preservesPartitioning=True)
            keyed_right = right.rdd.map(lambda row: (build_key(row, right_key_indexes), row), preservesPartitioning=True)
        
            # remove redundant key fields from the right
            def fixup_right(row, indexes):
                val = row[1]
                for i in indexes:
                    val.pop(i)
                return (row[0], val)
            keyed_right = keyed_right.map(lambda row: fixup_right(row, right_key_indexes), preservesPartitioning=True)

            if how == 'inner':
                joined = keyed_left.join(keyed_right)
            elif how == 'left':
                joined = keyed_left.leftOuterJoin(keyed_right)
            elif how == 'right':
                joined = keyed_left.rightOuterJoin(keyed_right)

            # throw away key now
            pairs = joined.map(lambda row: row[1], preservesPartitioning=True)
#            print 'result pairs', pairs.collect()

        res = pairs.map(lambda row: combine_results(row[0], row[1], left_count, right_count), preservesPartitioning=True)
#        print
#        print 'res', res.collect()
#        print 'new_col_names', new_col_names
#        print 'new_col_types', new_col_types

        self._exit(res, new_col_names, new_col_types)
        return self._rv(res, new_col_names, new_col_types)

    def unique(self):
        """
        Remove duplicate rows of the XFrame. Will not necessarily preserve the
        order of the given XFrame in the new XFrame.
        """

        self._entry()
        as_json = self.rdd.map(lambda row: json.dumps(row), preservesPartitioning=True)
        unique_rows = as_json.distinct()
        res = unique_rows.map(lambda s: json.loads(s), preservesPartitioning=True)
        self._exit(res)
        return self._rv(res)

    def sort(self, sort_column_names, sort_column_orders):
        """
        Sort current XFrame by the given columns, using the given sort order.
        Only columns that are type of str, int and float can be sorted.

        sort_column_orders is an array of boolean; True is ascending
        """
        self._entry(sort_column_names, sort_column_orders)

        sort_column_indexes = [self.col_names.index(name) for name in sort_column_names]
        key_fn = lambda row: CmpRows(row, sort_column_indexes, sort_column_orders)

        res = self.rdd.sortBy(keyfunc=key_fn)
        self._exit(res)
        return self._rv(res)



