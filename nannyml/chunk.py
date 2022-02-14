# Author:   Niels Nuyttens  <niels@nannyml.com>
#           Jakub Bialek    <jakub@nannyml.com>
#
# License: Apache Software License 2.0

"""NannyML module providing intelligent splitting of data into chunks."""

import abc
import logging
from typing import List

import numpy as np
import pandas as pd
from dateutil.parser import ParserError  # type: ignore
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import PolynomialFeatures

from nannyml.exceptions import ChunkerException, InvalidArgumentsException, MissingMetadataException
from nannyml.metadata import (
    NML_METADATA_GROUND_TRUTH_COLUMN_NAME,
    NML_METADATA_PARTITION_COLUMN_NAME,
    NML_METADATA_PREDICTION_COLUMN_NAME,
    NML_METADATA_TIMESTAMP_COLUMN_NAME,
)

logger = logging.getLogger(__name__)


class Chunk:
    """A subset of data that acts as a logical unit during calculations."""

    def __init__(self, key: str, data: pd.DataFrame, partition: str = None):
        """Creates a new chunk.

        Parameters
        ----------
        key : str, required.
            A value describing what data is wrapped in this chunk.
        data : DataFrame, required
            The data to be contained within the chunk
        partition : string, optional
            The 'partition' this chunk belongs to, for example 'reference' or 'analysis'.
        """
        self.key = key
        self.data = data
        self.partition = partition

        self.is_transition: bool = False

    def __repr__(self):
        """Returns textual summary of a chunk.

        Returns
        -------
        chunk_str: str

        """
        return (
            f'Chunk[key={self.key}, data=pd.DataFrame[[{self.data.shape[0]}x{self.data.shape[1]}]], '
            f'partition={self.partition}, is_transition={self.is_transition}]'
        )

    def __len__(self):
        """Returns the number of rows held within this chunk.

        Returns
        -------
        length: int
            Number of rows in the `data` property of the chunk.

        """
        return self.data.shape[0]


def _minimum_chunk_count(
    data: pd.DataFrame,
    prediction_column_name: str = NML_METADATA_PREDICTION_COLUMN_NAME,
    ground_truth_column_name: str = NML_METADATA_GROUND_TRUTH_COLUMN_NAME,
    lower_threshold: int = 300,
) -> int:
    def get_prediction(X):
        # model data
        h_coefs = [
            0.00000000e00,
            -3.46098897e04,
            2.65871679e04,
            3.46098897e04,
            2.29602791e04,
            -4.96886646e04,
            -1.12777343e-10,
            -2.29602791e04,
            3.13775672e-10,
            2.48718826e04,
        ]
        h_intercept = 1421.9522967076875
        transformation = PolynomialFeatures(3)
        #

        inputs = np.asarray(X)
        transformed_inputs = transformation.fit_transform(inputs)
        prediction = np.dot(transformed_inputs, h_coefs)[0] + h_intercept

        return prediction

    class_balance = np.mean(data[ground_truth_column_name])
    auc = roc_auc_score(data[ground_truth_column_name], data[prediction_column_name])
    chunk_size = get_prediction([[class_balance, auc]])
    chunk_size = np.maximum(lower_threshold, chunk_size)
    chunk_size = np.round(chunk_size, -2)
    minimum_chunk_size = int(chunk_size)

    return minimum_chunk_size


def _get_partition(c: Chunk, partition_column_name: str = NML_METADATA_PARTITION_COLUMN_NAME):
    if partition_column_name not in c.data.columns:
        raise MissingMetadataException(
            f"missing partition column '{NML_METADATA_PARTITION_COLUMN_NAME}'."
            "Please extract metadata for your DataFrame first."
        )

    if _is_transition(c, partition_column_name):
        return None

    return c.data[partition_column_name].iloc[0]


def _is_transition(c: Chunk, partition_column_name: str = NML_METADATA_PARTITION_COLUMN_NAME) -> bool:
    if c.data.shape[0] > 1:
        return c.data[partition_column_name].nunique() > 1
    else:
        return False


class Chunker(abc.ABC):
    """Base class for Chunker implementations.

    Inheriting classes will split a DataFrame into a list of Chunks.
    They will do this based on several constraints, e.g. observation timestamps, number of observations per Chunk
    or a preferred number of Chunks.
    """

    def __init__(self):
        """Creates a new Chunker. Not used directly."""
        pass

    def split(self, data: pd.DataFrame, columns=None) -> List[Chunk]:
        """Splits a given data frame into a list of chunks.

        This method provides a uniform interface across Chunker implementations to keep them interchangeable.

        After performing the implementation-specific `_split` method, there are some checks on the resulting chunk list.

        If the total number of chunks is low a warning will be written out to the logs.

        We dynamically determine the optimal minimum number of observations per chunk and then check if the resulting
        chunks contain at least as many. If there are any underpopulated chunks a warning will be written out in
        the logs.

        Parameters
        ----------
        data: DataFrame
            The data to be split into chunks
        columns: List[str]
            A list of columns to be included in the resulting chunk data. Unlisted columns will be dropped.

        Returns
        -------
        chunks: List[Chunk]
            The list of chunks

        """
        try:
            chunks = self._split(data)
        except Exception as exc:
            raise ChunkerException(f"could not split data into chunks: {exc}")

        for c in chunks:
            if _is_transition(c):
                c.is_transition = True

            c.partition = _get_partition(c)

            if columns is not None:
                c.data = c.data[columns]

        if len(chunks) < 6:
            # TODO wording
            logger.warning(
                'The resulting number of chunks is too low.'
                'Please consider splitting your data in a different way or continue at your own risk.'
            )

        # check if all chunk sizes > minimal chunk size. If not, render a warning message.
        underpopulated_chunks = [c for c in chunks if len(c) < _minimum_chunk_count(data)]

        if len(underpopulated_chunks) > 0:
            # TODO wording
            logger.warning(
                f'The resulting list of chunks contains {len(underpopulated_chunks)} underpopulated chunks.'
                'They contain too few records to be statistically relevant and might negatively influence '
                'the quality of calculations.'
                'Please consider splitting your data in a different way or continue at your own risk.'
            )

        return chunks

    # TODO wording
    @abc.abstractmethod
    def _split(self, data: pd.DataFrame) -> List[Chunk]:
        """Splits the DataFrame into chunks.

        Abstract method, to be implemented within inheriting classes.

        Parameters
        ----------
        data: pandas.DataFrame
            The full dataset that should be split into Chunks

        Returns
        -------
        chunks: array of Chunks
            The array of Chunks after splitting the original DataFrame `data`

        See Also
        --------
        PeriodBasedChunker: Splits data based on the timestamp of observations
        SizeBasedChunker: Splits data based on the amount of observations in a Chunk
        CountBasedChunker: Splits data based on the resulting number of Chunks

        Notes
        -----
        There is a minimal number of observations that a Chunk should contain in order to retain statistical relevance.
        A chunker will log a warning message when your splitting criteria would result in underpopulated chunks.
        Note that in this situation calculation results may not be relevant.

        """
        pass  # pragma: no cover


class PeriodBasedChunker(Chunker):
    """A Chunker that will split data into Chunks based on a date column in the data.

    Examples
    --------
    Chunk using monthly periods and providing a column name

    >>> from nannyml.chunk import PeriodBasedChunker
    >>> df = pd.read_parquet('/path/to/my/data.pq')
    >>> chunker = PeriodBasedChunker(date_column_name='observation_date', offset='M')
    >>> chunks = chunker.split(data=df)

    Or chunk using weekly periods

    >>> from nannyml.chunk import PeriodBasedChunker
    >>> df = pd.read_parquet('/path/to/my/data.pq')
    >>> chunker = PeriodBasedChunker(date_column=df['observation_date'], offset='W')
    >>> chunks = chunker.split(data=df)

    """

    def __init__(
        self,
        date_column_name: str = NML_METADATA_TIMESTAMP_COLUMN_NAME,
        offset: str = 'W',
    ):
        """Creates a new PeriodBasedChunker.

        Parameters
        ----------
        date_column_name: string
            The name of the column in the DataFrame that contains the date used for chunking.
            Defaults to the metadata timestamp column added by the `ModelMetadata.extract_metadata` function.

        offset: a frequency string representing a pandas.tseries.offsets.DateOffset
            The offset determines how the time-based grouping will occur. A list of possible values
            is to be found at https://pandas.pydata.org/docs/user_guide/timeseries.html#offset-aliases.

        Returns
        -------
        chunker: a PeriodBasedChunker instance used to split data into time-based Chunks.
        """
        super().__init__()

        self.date_column_name = date_column_name
        self.offset = offset

    def _split(self, data: pd.DataFrame) -> List[Chunk]:
        chunks = []
        date_column_name = self.date_column_name or self.date_column.name  # type: ignore
        try:
            grouped_data = data.groupby(pd.to_datetime(data[date_column_name]).dt.to_period(self.offset))
            for k in grouped_data.groups.keys():
                chunks.append(Chunk(key=str(k), data=grouped_data.get_group(k)))
        except KeyError:
            raise ChunkerException(f"could not find date_column '{date_column_name}' in given data")

        except ParserError:
            raise ChunkerException(
                f"could not parse date_column '{date_column_name}' values as dates."
                f"Please verify if you've specified the correct date column."
            )

        return chunks


class SizeBasedChunker(Chunker):
    """A Chunker that will split data into Chunks based on the preferred number of observations per Chunk.

    Notes
    -----
    - Chunks are adjacent, not overlapping
    - There will be no "incomplete chunks", so the leftover observations that cannot fill an entire chunk will
      be dropped by default.

    Examples
    --------
    Chunk using monthly periods and providing a column name

    >>> from nannyml.chunk import SizeBasedChunker
    >>> df = pd.read_parquet('/path/to/my/data.pq')
    >>> chunker = SizeBasedChunker(chunk_size=2000)
    >>> chunks = chunker.split(data=df)

    """

    def __init__(self, chunk_size: int):
        """Create a new SizeBasedChunker.

        Parameters
        ----------
        chunk_size: int
            The preferred size of the resulting Chunks, i.e. the number of observations in each Chunk.

        Returns
        -------
        chunker: a size-based instance used to split data into Chunks of a constant size.

        """
        super().__init__()

        # TODO wording
        if not isinstance(chunk_size, int):
            raise InvalidArgumentsException(
                f"given chunk_size is of type {type(chunk_size)} but should be an int."
                f"Please provide an integer as a chunk size"
            )

        # TODO wording
        if chunk_size <= 0:
            raise InvalidArgumentsException(
                f"given chunk_size {chunk_size} is less then or equal to zero."
                f"The chunk size should always be larger then zero"
            )

        self.chunk_size = chunk_size

    def _split(self, data: pd.DataFrame) -> List[Chunk]:
        data = data.copy().reset_index()
        chunks = [
            Chunk(key=f'[{i}:{i + self.chunk_size - 1}]', data=data.loc[i : i + self.chunk_size - 1, :])
            for i in range(0, len(data), self.chunk_size)
            if i + self.chunk_size - 1 < len(data)
        ]

        return chunks


class CountBasedChunker(Chunker):
    """A Chunker that will split data into chunks based on the preferred number of observations per chunk.

    Examples
    --------
    >>> from nannyml.chunk import CountBasedChunker
    >>> df = pd.read_parquet('/path/to/my/data.pq')
    >>> chunker = CountBasedChunker(chunk_count=100)
    >>> chunks = chunker.split(data=df)

    """

    def __init__(self, chunk_count: int):
        """Creates a new CountBasedChunker.

        It will calculate the amount of observations per chunk based on the given chunk count.
        It then continues to split the data into chunks just like a SizeBasedChunker does.

        Parameters
        ----------
        chunk_count: int
            The amount of chunks to split the data in.


        Returns
        -------
        chunker: CountBasedChunker

        """
        super().__init__()

        # TODO wording
        if not isinstance(chunk_count, int):
            raise InvalidArgumentsException(
                f"given chunk_count is of type {type(chunk_count)} but should be an int."
                f"Please provide an integer as a chunk count"
            )

        # TODO wording
        if chunk_count <= 0:
            raise InvalidArgumentsException(
                f"given chunk_count {chunk_count} is less then or equal to zero."
                f"The chunk count should always be larger then zero"
            )

        self.chunk_count = chunk_count

    def _split(self, data: pd.DataFrame) -> List[Chunk]:
        if data.shape[0] == 0:
            return []

        data = data.copy().reset_index()

        chunk_size = data.shape[0] // self.chunk_count
        chunks = [
            Chunk(key=f'[{i}:{i + chunk_size - 1}]', data=data.loc[i : i + chunk_size - 1, :])
            for i in range(0, len(data), chunk_size)
            if i + chunk_size - 1 < len(data)
        ]
        return chunks