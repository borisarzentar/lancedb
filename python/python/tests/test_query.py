# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright The LanceDB Authors

import unittest.mock as mock
from datetime import timedelta

import lancedb
from lancedb.index import IvfPq
import numpy as np
import pandas.testing as tm
import pyarrow as pa
import pytest
import pytest_asyncio
from lancedb.pydantic import LanceModel, Vector
from lancedb.query import AsyncQueryBase, LanceVectorQueryBuilder, Query
from lancedb.table import AsyncTable, LanceTable


@pytest.fixture(scope="module")
def table(tmpdir_factory) -> lancedb.table.Table:
    tmp_path = str(tmpdir_factory.mktemp("data"))
    db = lancedb.connect(tmp_path)
    df = pa.table(
        {
            "vector": pa.array(
                [[1, 2], [3, 4]], type=pa.list_(pa.float32(), list_size=2)
            ),
            "id": pa.array([1, 2]),
            "str_field": pa.array(["a", "b"]),
            "float_field": pa.array([1.0, 2.0]),
        }
    )
    return db.create_table("test", df)


@pytest_asyncio.fixture
async def table_async(tmp_path) -> AsyncTable:
    conn = await lancedb.connect_async(
        tmp_path, read_consistency_interval=timedelta(seconds=0)
    )
    data = pa.table(
        {
            "vector": pa.array(
                [[1, 2], [3, 4]], type=pa.list_(pa.float32(), list_size=2)
            ),
            "id": pa.array([1, 2]),
            "str_field": pa.array(["a", "b"]),
            "float_field": pa.array([1.0, 2.0]),
        }
    )
    return await conn.create_table("test", data)


def test_cast(table):
    class TestModel(LanceModel):
        vector: Vector(2)
        id: int
        str_field: str
        float_field: float

    q = LanceVectorQueryBuilder(table, [0, 0], "vector").limit(1)
    results = q.to_pydantic(TestModel)
    assert len(results) == 1
    r0 = results[0]
    assert isinstance(r0, TestModel)
    assert r0.id == 1
    assert r0.vector == [1, 2]
    assert r0.str_field == "a"
    assert r0.float_field == 1.0


def test_offset(table):
    results_without_offset = LanceVectorQueryBuilder(table, [0, 0], "vector")
    assert len(results_without_offset.to_pandas()) == 2
    results_with_offset = LanceVectorQueryBuilder(table, [0, 0], "vector").offset(1)
    assert len(results_with_offset.to_pandas()) == 1


def test_query_builder(table):
    rs = (
        LanceVectorQueryBuilder(table, [0, 0], "vector")
        .limit(1)
        .select(["id", "vector"])
        .to_list()
    )
    assert rs[0]["id"] == 1
    assert all(np.array(rs[0]["vector"]) == [1, 2])


def test_with_row_id(table: lancedb.table.Table):
    rs = table.search().with_row_id(True).to_arrow()
    assert "_rowid" in rs.column_names
    assert rs["_rowid"].to_pylist() == [0, 1]


def test_vector_query_with_no_limit(table):
    with pytest.raises(ValueError):
        LanceVectorQueryBuilder(table, [0, 0], "vector").limit(0).select(
            ["id", "vector"]
        ).to_list()

    with pytest.raises(ValueError):
        LanceVectorQueryBuilder(table, [0, 0], "vector").limit(None).select(
            ["id", "vector"]
        ).to_list()


def test_query_builder_batches(table):
    rs = (
        LanceVectorQueryBuilder(table, [0, 0], "vector")
        .limit(2)
        .select(["id", "vector"])
        .to_batches(1)
    )
    rs_list = []
    for item in rs:
        rs_list.append(item)
        assert isinstance(item, pa.RecordBatch)
    assert len(rs_list) == 1
    assert len(rs_list[0]["id"]) == 2
    assert all(rs_list[0].to_pandas()["vector"][0] == [1.0, 2.0])
    assert rs_list[0].to_pandas()["id"][0] == 1
    assert all(rs_list[0].to_pandas()["vector"][1] == [3.0, 4.0])
    assert rs_list[0].to_pandas()["id"][1] == 2


def test_dynamic_projection(table):
    rs = (
        LanceVectorQueryBuilder(table, [0, 0], "vector")
        .limit(1)
        .select({"id": "id", "id2": "id * 2"})
        .to_list()
    )
    assert rs[0]["id"] == 1
    assert rs[0]["id2"] == 2


def test_query_builder_with_filter(table):
    rs = LanceVectorQueryBuilder(table, [0, 0], "vector").where("id = 2").to_list()
    assert rs[0]["id"] == 2
    assert all(np.array(rs[0]["vector"]) == [3, 4])


def test_query_builder_with_prefilter(table):
    df = (
        LanceVectorQueryBuilder(table, [0, 0], "vector")
        .where("id = 2")
        .limit(1)
        .to_pandas()
    )
    assert len(df) == 0

    df = (
        LanceVectorQueryBuilder(table, [0, 0], "vector")
        .where("id = 2", prefilter=True)
        .limit(1)
        .to_pandas()
    )
    assert df["id"].values[0] == 2
    assert all(df["vector"].values[0] == [3, 4])


def test_query_builder_with_metric(table):
    query = [4, 8]
    vector_column_name = "vector"
    df_default = LanceVectorQueryBuilder(table, query, vector_column_name).to_pandas()
    df_l2 = (
        LanceVectorQueryBuilder(table, query, vector_column_name)
        .metric("L2")
        .to_pandas()
    )
    tm.assert_frame_equal(df_default, df_l2)

    df_cosine = (
        LanceVectorQueryBuilder(table, query, vector_column_name)
        .metric("cosine")
        .limit(1)
        .to_pandas()
    )
    assert df_cosine._distance[0] == pytest.approx(
        cosine_distance(query, df_cosine.vector[0]),
        abs=1e-6,
    )
    assert 0 <= df_cosine._distance[0] <= 1


def test_query_builder_with_different_vector_column():
    table = mock.MagicMock(spec=LanceTable)
    query = [4, 8]
    vector_column_name = "foo_vector"
    builder = (
        LanceVectorQueryBuilder(table, query, vector_column_name)
        .metric("cosine")
        .where("b < 10")
        .select(["b"])
        .limit(2)
    )
    ds = mock.Mock()
    table.to_lance.return_value = ds
    builder.to_arrow()
    table._execute_query.assert_called_once_with(
        Query(
            vector=query,
            filter="b < 10",
            k=2,
            metric="cosine",
            columns=["b"],
            nprobes=20,
            refine_factor=None,
            vector_column="foo_vector",
        ),
        None,
    )


def cosine_distance(vec1, vec2):
    return 1 - np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))


async def check_query(
    query: AsyncQueryBase, *, expected_num_rows=None, expected_columns=None
):
    num_rows = 0
    results = await query.to_batches()
    async for batch in results:
        if expected_columns is not None:
            assert batch.schema.names == expected_columns
        num_rows += batch.num_rows
    if expected_num_rows is not None:
        assert num_rows == expected_num_rows


@pytest.mark.asyncio
async def test_query_async(table_async: AsyncTable):
    await check_query(
        table_async.query(),
        expected_num_rows=2,
        expected_columns=["vector", "id", "str_field", "float_field"],
    )
    await check_query(table_async.query().where("id = 2"), expected_num_rows=1)
    await check_query(
        table_async.query().select(["id", "vector"]), expected_columns=["id", "vector"]
    )
    await check_query(
        table_async.query().select({"foo": "id", "bar": "id + 1"}),
        expected_columns=["foo", "bar"],
    )

    await check_query(table_async.query().limit(1), expected_num_rows=1)
    await check_query(table_async.query().offset(1), expected_num_rows=1)

    await check_query(
        table_async.query().nearest_to(pa.array([1, 2])), expected_num_rows=2
    )
    # Support different types of inputs for the vector query
    for vector_query in [
        [1, 2],
        [1.0, 2.0],
        np.array([1, 2]),
        (1, 2),
    ]:
        await check_query(
            table_async.query().nearest_to(vector_query), expected_num_rows=2
        )

    # No easy way to check these vector query parameters are doing what they say.  We
    # just check that they don't raise exceptions and assume this is tested at a lower
    # level.
    await check_query(
        table_async.query().where("id = 2").nearest_to(pa.array([1, 2])).postfilter(),
        expected_num_rows=1,
    )
    await check_query(
        table_async.query().nearest_to(pa.array([1, 2])).refine_factor(1),
        expected_num_rows=2,
    )
    await check_query(
        table_async.query().nearest_to(pa.array([1, 2])).nprobes(10),
        expected_num_rows=2,
    )
    await check_query(
        table_async.query().nearest_to(pa.array([1, 2])).bypass_vector_index(),
        expected_num_rows=2,
    )
    await check_query(
        table_async.query().nearest_to(pa.array([1, 2])).distance_type("dot"),
        expected_num_rows=2,
    )
    await check_query(
        table_async.query().nearest_to(pa.array([1, 2])).distance_type("DoT"),
        expected_num_rows=2,
    )

    # Make sure we can use a vector query as a base query (e.g. call limit on it)
    # Also make sure `vector_search` works
    await check_query(table_async.vector_search([1, 2]).limit(1), expected_num_rows=1)

    # Also check an empty query
    await check_query(table_async.query().where("id < 0"), expected_num_rows=0)

    # with row id
    await check_query(
        table_async.query().select(["id", "vector"]).with_row_id(),
        expected_columns=["id", "vector", "_rowid"],
    )


@pytest.mark.asyncio
async def test_query_to_arrow_async(table_async: AsyncTable):
    table = await table_async.to_arrow()
    assert table.num_rows == 2
    assert table.num_columns == 4

    table = await table_async.query().to_arrow()
    assert table.num_rows == 2
    assert table.num_columns == 4

    table = await table_async.query().where("id < 0").to_arrow()
    assert table.num_rows == 0
    assert table.num_columns == 4


@pytest.mark.asyncio
async def test_query_to_pandas_async(table_async: AsyncTable):
    df = await table_async.to_pandas()
    assert df.shape == (2, 4)

    df = await table_async.query().to_pandas()
    assert df.shape == (2, 4)

    df = await table_async.query().where("id < 0").to_pandas()
    assert df.shape == (0, 4)


@pytest.mark.asyncio
async def test_none_query(table_async: AsyncTable):
    with pytest.raises(ValueError):
        await table_async.query().nearest_to(None).to_arrow()


@pytest.mark.asyncio
async def test_fast_search_async(tmp_path):
    db = await lancedb.connect_async(tmp_path)
    vectors = pa.FixedShapeTensorArray.from_numpy_ndarray(
        np.random.rand(256, 32)
    ).storage
    table = await db.create_table("test", pa.table({"vector": vectors}))
    await table.create_index(
        "vector", config=IvfPq(num_partitions=1, num_sub_vectors=1)
    )
    await table.add(pa.table({"vector": vectors}))

    q = [1.0] * 32
    plan = await table.query().nearest_to(q).explain_plan(True)
    assert "LanceScan" in plan
    plan = await table.query().nearest_to(q).fast_search().explain_plan(True)
    assert "LanceScan" not in plan


def test_explain_plan(table):
    q = LanceVectorQueryBuilder(table, [0, 0], "vector")
    plan = q.explain_plan(verbose=True)
    assert "KNN" in plan


@pytest.mark.asyncio
async def test_explain_plan_async(table_async: AsyncTable):
    plan = await table_async.query().nearest_to(pa.array([1, 2])).explain_plan(True)
    assert "KNN" in plan


@pytest.mark.asyncio
async def test_query_camelcase_async(tmp_path):
    db = await lancedb.connect_async(tmp_path)
    table = await db.create_table("test", pa.table({"camelCase": pa.array([1, 2])}))

    result = await table.query().select(["camelCase"]).to_arrow()
    assert result == pa.table({"camelCase": pa.array([1, 2])})


@pytest.mark.asyncio
async def test_query_to_list_async(table_async: AsyncTable):
    list = await table_async.query().to_list()
    assert len(list) == 2
    assert list[0]["vector"] == [1, 2]
    assert list[1]["vector"] == [3, 4]
