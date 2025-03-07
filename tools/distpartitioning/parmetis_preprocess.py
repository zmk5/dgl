import argparse
import logging
import os
import sys
from pathlib import Path

import constants

import numpy as np
import pyarrow
import pyarrow.csv as csv
import pyarrow.parquet as pq
import torch
import torch.distributed as dist
from utils import get_idranges, get_node_types, read_json
import array_readwriter


def get_proc_info():
    """Helper function to get the rank from the
    environment when `mpirun` is used to run this python program.

    Please note that for mpi(openmpi) installation the rank is retrieved from the
    environment using OMPI_COMM_WORLD_RANK. For mpich it is
    retrieved from the environment using PMI_RANK.

    Returns:
    --------
    integer :
        Rank of the current process.
    """
    env_variables = dict(os.environ)
    # mpich
    if "PMI_RANK" in env_variables:
        return int(env_variables["PMI_RANK"])
    # openmpi
    elif "OMPI_COMM_WORLD_RANK" in env_variables:
        return int(env_variables["OMPI_COMM_WORLD_RANK"])
    else:
        return 0


def gen_edge_files(schema_map, output):
    """Function to create edges files to be consumed by ParMETIS
    for partitioning purposes.

    This function creates the edge files and each of these will have the
    following format (meaning each line of these file is of the following format)
    <global_src_id> <global_dst_id>

    Here ``global`` prefix means that globally unique identifier assigned each node
    in the input graph. In this context globally unique means unique across all the
    nodes in the input graph.

    Parameters:
    -----------
    schema_map : json dictionary
        Dictionary created by reading the metadata.json file for the input dataset.
    output : string
        Location of storing the node-weights and edge files for ParMETIS.
    """
    rank = get_proc_info()
    type_nid_dict, ntype_gnid_offset = get_idranges(
        schema_map[constants.STR_NODE_TYPE],
        schema_map[constants.STR_NUM_NODES_PER_CHUNK],
    )

    # Regenerate edge files here.
    edge_data = schema_map[constants.STR_EDGES]
    etype_names = schema_map[constants.STR_EDGE_TYPE]
    etype_name_idmap = {e: idx for idx, e in enumerate(etype_names)}
    edge_tids, _ = get_idranges(
        schema_map[constants.STR_EDGE_TYPE],
        schema_map[constants.STR_NUM_EDGES_PER_CHUNK],
    )

    outdir = Path(output)
    os.makedirs(outdir, exist_ok=True)
    edge_files = []
    num_parts = len(schema_map[constants.STR_NUM_EDGES_PER_CHUNK][0])
    for etype_name, etype_info in edge_data.items():

        edges_format = etype_info[constants.STR_FORMAT][constants.STR_NAME]
        edge_data_files = etype_info[constants.STR_DATA]

        # ``edgetype`` strings are in canonical format, src_node_type:edge_type:dst_node_type
        tokens = etype_name.split(":")
        assert len(tokens) == 3

        src_ntype_name = tokens[0]
        rel_name = tokens[1]
        dst_ntype_name = tokens[2]

        def convert_to_numpy_and_write_back(data_df):
            data_f0 = data_df["f0"].to_numpy()
            data_f1 = data_df["f1"].to_numpy()

            global_src_id = data_f0 + ntype_gnid_offset[src_ntype_name][0, 0]
            global_dst_id = data_f1 + ntype_gnid_offset[dst_ntype_name][0, 0]
            cols = [global_src_id, global_dst_id]
            col_names = ["global_src_id", "global_dst_id"]

            out_file = edge_data_files[rank].split("/")[-1]
            out_file = os.path.join(outdir, "edges_{}".format(out_file))

            # TODO(thvasilo): We should support writing to the same format as the input
            options = csv.WriteOptions(include_header=False, delimiter=" ")
            options.delimiter = " "
            csv.write_csv(
                pyarrow.Table.from_arrays(cols, names=col_names),
                out_file,
                options,
            )
            return out_file

        if edges_format == constants.STR_CSV:
            delimiter = etype_info[constants.STR_FORMAT][
                constants.STR_FORMAT_DELIMITER
            ]
            data_df = csv.read_csv(
                edge_data_files[rank],
                read_options=pyarrow.csv.ReadOptions(
                    autogenerate_column_names=True
                ),
                parse_options=pyarrow.csv.ParseOptions(delimiter=delimiter),
            )
        elif edges_format == constants.STR_PARQUET:
            data_df = pq.read_table(edge_data_files[rank])
            data_df = data_df.rename_columns(["f0", "f1"])
        else:
            raise NotImplementedError(f"Unknown edge format {edges_format}")

        out_file = convert_to_numpy_and_write_back(data_df)
        edge_files.append(out_file)

    return edge_files


def read_node_features(schema_map, tgt_ntype_name, feat_names, input_dir):
    """Helper function to read the node features.
    Only node features which are requested are read from the input dataset.

    Parameters:
    -----------
    schema_map : json dictionary
        Dictionary created by reading the metadata.json file for the input dataset.
    tgt_ntype_name : string
        node-type name, for which node features will be read from the input dataset.
    feat_names : set
        A set of strings, feature names, which will be read for a given node type.
    input_dir : str
        The input directory where the dataset is located.

    Returns:
    --------
    dictionary :
        A dictionary where key is the feature-name and value is the numpy array.
    """
    rank = get_proc_info()
    node_features = {}
    if constants.STR_NODE_DATA in schema_map:
        dataset_features = schema_map[constants.STR_NODE_DATA]
        if dataset_features and (len(dataset_features) > 0):
            for ntype_name, ntype_feature_data in dataset_features.items():
                if ntype_name != tgt_ntype_name:
                    continue
                # ntype_feature_data is a dictionary
                # where key: feature_name, value: dictionary in which keys are "format", "data".
                for feat_name, feat_data in ntype_feature_data.items():
                    if feat_name in feat_names:
                        feat_data_fname = feat_data[constants.STR_DATA][rank]
                        if not os.path.isabs(feat_data_fname):
                            feat_data_fname = os.path.join(input_dir, feat_data_fname)
                        logging.info(f"Reading: {feat_data_fname}")
                        file_suffix = Path(feat_data_fname).suffix
                        reader_fmt_meta = {
                            "name": file_suffix[1:]
                        }
                        node_features[feat_name] = array_readwriter.get_array_parser(
                            **reader_fmt_meta).read(feat_data_fname)
    return node_features


def gen_node_weights_files(schema_map, input_dir, output):
    """Function to create node weight files for ParMETIS along with the edge files.

    This function generates node-data files, which will be read by the ParMETIS
    executable for partitioning purposes. Each line in these files will be of the
    following format:
        <node_type_id> <node_weight_list> <type_wise_node_id>
    node_type_id -  is id assigned to the node-type to which a given particular
        node belongs to
    weight_list - this is a one-hot vector in which the number in the location of
        the current nodes' node-type will be set to `1` and other will be `0`
    type_node_id - this is the id assigned to the node (in the context of the current
        nodes` node-type). Meaning this id is unique across all the nodes which belong to
        the current nodes` node-type.

    Parameters:
    -----------
    schema_map : json dictionary
        Dictionary created by reading the metadata.json file for the input dataset.
    input_dir : str
        The input directory where the dataset is located.
    output : string
        Location of storing the node-weights and edge files for ParMETIS.

    Returns:
    --------
    list :
        List of filenames for nodes of the input graph.
    list :
        List o ffilenames for edges of the input graph.
    """
    rank = get_proc_info()
    ntypes_ntypeid_map, ntypes, ntid_ntype_map = get_node_types(schema_map)
    type_nid_dict, ntype_gnid_offset = get_idranges(
        schema_map[constants.STR_NODE_TYPE],
        schema_map[constants.STR_NUM_NODES_PER_CHUNK],
    )

    node_files = []
    outdir = Path(output)
    os.makedirs(outdir, exist_ok=True)

    for ntype_id, ntype_name in ntid_ntype_map.items():
        type_start, type_end = (
            type_nid_dict[ntype_name][rank][0],
            type_nid_dict[ntype_name][rank][1],
        )
        count = type_end - type_start
        sz = (count,)

        cols = []
        col_names = []
        cols.append(
            pyarrow.array(np.ones(sz, dtype=np.int64) * np.int64(ntype_id))
        )
        col_names.append("ntype")

        for i in range(len(ntypes)):
            if i == ntype_id:
                cols.append(pyarrow.array(np.ones(sz, dtype=np.int64)))
            else:
                cols.append(pyarrow.array(np.zeros(sz, dtype=np.int64)))
            col_names.append("w{}".format(i))

        # Add train/test/validation masks if present. node-degree will be added when this file
        # is read by ParMETIS to mimic the exisiting single process pipeline present in dgl.
        node_feats = read_node_features(
            schema_map, ntype_name, set(["train_mask", "val_mask", "test_mask"]),
            input_dir
        )
        for k, v in node_feats.items():
            assert sz == v.shape
            cols.append(pyarrow.array(v))
            col_names.append(k)

        # `type_nid` should be the very last column in the node weights files.
        cols.append(
            pyarrow.array(
                np.arange(count, dtype=np.int64) + np.int64(type_start)
            )
        )
        col_names.append("type_nid")

        out_file = os.path.join(
            outdir, "node_weights_{}_{}.txt".format(ntype_name, rank)
        )
        options = csv.WriteOptions(include_header=False, delimiter=" ")
        options.delimiter = " "

        csv.write_csv(
            pyarrow.Table.from_arrays(cols, names=col_names), out_file, options
        )
        node_files.append(
            (
                ntype_gnid_offset[ntype_name][0, 0] + type_start,
                ntype_gnid_offset[ntype_name][0, 0] + type_end,
                out_file,
            )
        )

    return node_files


def gen_parmetis_input_args(params, schema_map):
    """Function to create two input arguments which will be passed to the parmetis.
    first argument is a text file which has a list of node-weights files,
    namely parmetis-nfiles.txt, and second argument is a text file which has a
    list of edge files, namely parmetis_efiles.txt.
    ParMETIS uses these two files to read/load the graph and partition the graph
    With regards to the file format, parmetis_nfiles.txt uses the following format
    for each line in that file:
        <filename> <global_node_id_start> <global_node_id_end>(exclusive)
    While parmetis_efiles.txt just has <filename> in each line.

    Parameters:
    -----------
    params : argparser instance
        Instance of ArgParser class, which has all the input arguments passed to
        run this program.
    schema_map : json dictionary
        Dictionary object created after reading the graph metadata.json file.
    """

    num_nodes_per_chunk = schema_map[constants.STR_NUM_NODES_PER_CHUNK]
    # TODO: This makes the assumption that all node files have the same number of chunks
    num_node_parts = len(num_nodes_per_chunk[0])
    ntypes_ntypeid_map, ntypes, ntid_ntype_map = get_node_types(schema_map)
    type_nid_dict, ntype_gnid_offset = get_idranges(
        schema_map[constants.STR_NODE_TYPE],
        schema_map[constants.STR_NUM_NODES_PER_CHUNK],
    )

    # Check if <graph-name>_stats.txt exists, if not create one using metadata.
    # Here stats file will be created in the current directory.
    # No. of constraints, third column in the stats file is computed as follows:
    #   num_constraints = no. of node types + train_mask + test_mask + val_mask
    #   Here, (train/test/val) masks will be set to 1 if these masks exist for
    #   all the node types in the graph, otherwise these flags will be set to 0
    assert (
        constants.STR_GRAPH_NAME in schema_map
    ), "Graph name is not present in the json file"
    graph_name = schema_map[constants.STR_GRAPH_NAME]
    if not os.path.isfile(f"{graph_name}_stats.txt"):
        num_nodes = np.sum(
            np.concatenate(schema_map[constants.STR_NUM_NODES_PER_CHUNK])
        )
        num_edges = np.sum(
            np.concatenate(schema_map[constants.STR_NUM_EDGES_PER_CHUNK])
        )
        num_ntypes = len(schema_map[constants.STR_NODE_TYPE])

        train_mask = test_mask = val_mask = 0
        node_feats = schema_map[constants.STR_NODE_DATA]
        for ntype, ntype_data in node_feats.items():
            if "train_mask" in ntype_data:
                train_mask += 1
            if "test_mask" in ntype_data:
                test_mask += 1
            if "val_mask" in ntype_data:
                val_mask += 1
        train_mask = train_mask // num_ntypes
        test_mask = test_mask // num_ntypes
        val_mask = val_mask // num_ntypes
        num_constraints = num_ntypes + train_mask + test_mask + val_mask

        with open(f"{graph_name}_stats.txt", "w") as sf:
            sf.write(f"{num_nodes} {num_edges} {num_constraints}")

    node_files = []
    outdir = Path(params.output_dir)
    os.makedirs(outdir, exist_ok=True)
    for ntype_id, ntype_name in ntid_ntype_map.items():
        global_nid_offset = ntype_gnid_offset[ntype_name][0, 0]
        for r in range(num_node_parts):
            type_start, type_end = (
                type_nid_dict[ntype_name][r][0],
                type_nid_dict[ntype_name][r][1],
            )
            out_file = os.path.join(
                outdir, "node_weights_{}_{}.txt".format(ntype_name, r)
            )
            node_files.append(
                (
                    out_file,
                    global_nid_offset + type_start,
                    global_nid_offset + type_end,
                )
            )

    nfile = open(os.path.join(params.output_dir, "parmetis_nfiles.txt"), "w")
    for f in node_files:
        # format: filename global_node_id_start global_node_id_end(exclusive)
        nfile.write("{} {} {}\n".format(f[0], f[1], f[2]))
    nfile.close()

    # Regenerate edge files here.
    edge_data = schema_map[constants.STR_EDGES]
    edge_files = []
    for etype_name, etype_info in edge_data.items():
        edge_data_files = etype_info[constants.STR_DATA]
        for edge_file_path in edge_data_files:
            out_file = os.path.basename(edge_file_path)
            out_file = os.path.join(outdir, "edges_{}".format(out_file))
            edge_files.append(out_file)

    with open(
        os.path.join(params.output_dir, "parmetis_efiles.txt"), "w"
    ) as efile:
        for f in edge_files:
            efile.write("{}\n".format(f))


def run_preprocess_data(params):
    """Main function which will help create graph files for ParMETIS processing

    Parameters:
    -----------
    params : argparser object
        An instance of argparser class which stores command line arguments.
    """
    logging.info(f"Starting to generate ParMETIS files...")

    rank = get_proc_info()
    schema_map = read_json(params.schema_file)
    num_nodes_per_chunk = schema_map[constants.STR_NUM_NODES_PER_CHUNK]
    num_parts = len(num_nodes_per_chunk[0])
    gen_node_weights_files(schema_map, params.input_dir, params.output_dir)
    logging.info(f"Done with node weights....")

    gen_edge_files(schema_map, params.output_dir)
    logging.info(f"Done with edge weights...")

    if rank == 0:
        gen_parmetis_input_args(params, schema_map)
    logging.info(f"Done generating files for ParMETIS run ..")


if __name__ == "__main__":
    """Main function used to generate temporary files needed for ParMETIS execution.
    This function generates node-weight files and edges files which are consumed by ParMETIS.

    Example usage:
    --------------
    mpirun -np 4 python3 parmetis_preprocess.py --schema <file> --output <target-output-dir>
    """
    parser = argparse.ArgumentParser(
        description="Generate ParMETIS files for input dataset"
    )
    parser.add_argument(
        "--schema_file",
        required=True,
        type=str,
        help="The schema of the input graph",
    )
    parser.add_argument(
        "--input_dir",
        type=str,
        help="The input directory where the dataset is located",
    )
    parser.add_argument(
        "--output_dir",
        required=True,
        type=str,
        help="The output directory for the node weights files and auxiliary files for ParMETIS.",
    )
    params = parser.parse_args()

    # Invoke the function to generate files for parmetis
    run_preprocess_data(params)
