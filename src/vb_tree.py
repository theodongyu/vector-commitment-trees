import sys
import blst
import hashlib
from poly_utils import PrimeField
from kzg_utils import KzgUtils
from fft import fft
from time import time
from random import randint, shuffle

# General functions


def int_to_bytes(x: int) -> bytes:
    return x.to_bytes(32, "little")


def int_from_bytes(x: bytes) -> int:
    return int.from_bytes(x, "little")


def hash(x):
    if isinstance(x, bytes):
        return hashlib.sha256(x).digest()
    elif isinstance(x, blst.P1):
        return hash(x.compress())
    b = b""
    for a in x:
        if isinstance(a, tuple):
            b += hash(a)
        if isinstance(a, bytes):
            b += a
        elif isinstance(a, int):
            b += a.to_bytes(32, "little")
        elif isinstance(a, blst.P1):
            b += hash(a.compress())
    return hash(b)


def hash_to_int(x):
    return int.from_bytes(hash(x), "little")


class KzgIntegration:
    def __init__(self, secret: int, modulus: int, width: int, primitive_root: int):
        self.modulus = modulus
        self.width = width
        assert pow(primitive_root, (modulus - 1) // width, modulus) != 1
        assert pow(primitive_root, modulus - 1, modulus) == 1
        self.root_of_unity = pow(
            primitive_root, (modulus - 1) // width, modulus)
        self.setup = self._generate_setup(width, secret)

    def _generate_setup(self, size, secret):
        """
        Generates a setup in the G1 group and G2 group, as well as the Lagrange polynomials in G1 (via FFT)
        """
        g1_setup = [blst.G1().mult(pow(secret, i, self.modulus))
                    for i in range(size)]
        g2_setup = [blst.G2().mult(pow(secret, i, self.modulus))
                    for i in range(size)]
        g1_lagrange = fft(g1_setup, self.modulus, self.root_of_unity, inv=True)
        return {"g1": g1_setup, "g2": g2_setup, "g1_lagrange": g1_lagrange}

    def kzg_utils(self):
        primefield = PrimeField(self.modulus, self.width)
        domain = [pow(self.root_of_unity, i, self.modulus)
                  for i in range(self.width)]
        return KzgUtils(self.modulus, self.width, domain, self.setup, primefield)


class VBTreeNode:
    def __init__(self, keys: list = None, values: list = None):
        self.keys = keys if keys is not None else []
        self.values = values if values is not None else []
        self.children = []
        self.hash = None
        self.commitment = blst.G1().mult(0)

    def node_hash(self):
        if self.is_leaf():
            self.hash = hash(self.keys + self.values)
        else:
            self.hash = hash([self.commitment.compress()] +
                             self.keys + self.values)

    def key_count(self):
        return len(self.keys)

    def child_count(self):
        return len(self.children)

    def is_leaf(self) -> bool:
        return self.children == []

    def show_key_values(self):
        return [(int_from_bytes(key), int_from_bytes(value)) for key, value in zip(self.keys, self.values)]


class VBTree:
    def __init__(self, kzg: KzgIntegration, root: VBTreeNode):
        self.kzg = kzg.kzg_utils()
        self.setup = kzg.setup
        self.root = root
        assert kzg.width // 2 >= 2
        self.min_degree = kzg.width // 2
        self.modulus = kzg.modulus
        self.width = kzg.width

    def _insert(self, node: VBTreeNode, key: bytes, value: bytes, update: bool):
        """
        Recursive insert operator
        """
        t = self.min_degree
        key_count = node.key_count()

        if key_count == (2 * t) - 1:
            raise Exception("Error, Node is full")

        idx = key_count - 1
        if node.is_leaf():
            while idx >= 0 and key < node.keys[idx]:
                idx -= 1
            node.keys.insert(idx + 1, key)
            node.values.insert(idx + 1, value)
        else:
            while idx >= 0 and key < node.keys[idx]:
                idx -= 1

            idx += 1
            if node.children[idx].key_count() == (2 * t) - 1:
                self._split_child(node, idx)
                if key > node.keys[idx]:
                    idx += 1
            self._insert(node.children[idx], key, value, update)

        return node

    def _split_child(self, node: VBTreeNode, idx: int):
        """
        Split a child node
        """

        t = self.min_degree

        child = node.children[idx]
        new_node = VBTreeNode()
        node.children.insert(idx + 1, new_node)

        node.keys.insert(idx, child.keys[t - 1])
        node.values.insert(idx, child.values[t - 1])

        new_node.keys = child.keys[t: (2 * t) - 1]
        new_node.values = child.values[t: (2 * t) - 1]
        child.keys = child.keys[0:t - 1]
        child.values = child.values[0:t - 1]

        if not child.is_leaf():
            new_node.children = child.children[t: (2 * t)]
            child.children = child.children[0: t]

    def insert_node(self, key: bytes, value: bytes, update: bool = False):
        """
        Insert a node into the tree
        """
        find_node = self.find_node(self.root, key)
        if find_node is not None:
            node, idx = find_node
            if update:
                node.values[idx] = value
            return

        t = self.min_degree
        root = self.root
        if root.key_count() == (2 * t) - 1:
            new_node = VBTreeNode()
            self.root = new_node
            new_node.children.insert(0, root)
            self._split_child(new_node, 0)
            self._insert(new_node, key, value, update)
        else:
            self._insert(root, key, value, update)

    def upsert_vc_node(self, key: bytes, value: bytes):
        """
        Insert or update a node in the tree and update the hashes/commitments
        """
        t = self.min_degree
        root = self.root

        path = self.find_path_to_node(root, key)
        last_node, last_idx = path[-1]

        # Update
        if last_idx < last_node.key_count() and last_node.keys[last_idx] == key:
            old_hash = last_node.hash
            last_node.values[last_idx] = value
            last_node.node_hash()
            new_hash = last_node.hash
            value_change = (int_from_bytes(
                new_hash) - int_from_bytes(old_hash) + self.modulus) % self.modulus

        # Insert
        else:
            splits = [True if path[i][0].key_count() == (
                2 * t) - 1 else False for i in range(len(path))]
            split_counts = splits.count(True)

            if split_counts == 0:
                old_hash = last_node.hash
                self.insert_node(key, value)
                last_node.node_hash()
                new_hash = last_node.hash
                value_change = (int_from_bytes(
                    new_hash) - int_from_bytes(old_hash) + self.modulus) % self.modulus

            else:
                self._insert_vc_node_splits(key, value, path, splits)
                return

        for node, idx in reversed(path):
            if node == last_node:
                continue
            old_hash = node.hash
            if node.commitment is None:
                self.add_node_hash(node)
            else:
                node.commitment.add(
                    self.setup["g1_lagrange"][idx].dup().mult(value_change))
                node.node_hash()
            new_hash = node.hash
            value_change = (int_from_bytes(
                new_hash) - int_from_bytes(old_hash) + self.modulus) % self.modulus

    def _insert_vc_node_splits(self, key: bytes, value: bytes, path: list, splits: list):
        """
        Handles the hash and commitment updates when nodes are split during insertion
        
        The method is divided into 3 parts
        1. Determine the the indexes of the updated nodes, the split nodes and the shifted nodes
        2. Re-determines the path based on the indexes found in part 1
        3. Update the hashes and commitments of the nodes at each level of the path from the bottom up
        """
        t = self.min_degree

        # Part 1: Determine the the indexes of the updated nodes, the split nodes and the shifted nodes
        update_path = []
        for i in range(len(path)):
            node, idx = path[i]
            node_type = 'leaf' if node.is_leaf() else 'inner'
            previous_node = path[i - 1][0]
            previous_idx = path[i - 1][1]
            hash = node.hash
            value_dict = {'node_type': node_type, 'hash': hash}
            if splits[i]:
                if i == 0: # Root node
                    value_dict['updated_idx'] = 1 if idx > t - 1 else 0
                    value_dict['split_idx'] = 0 if idx > t - 1 else 1
                else:
                    value_dict['updated_idx'] = previous_idx + \
                        1 if idx > t - 1 else previous_idx
                    value_dict['split_idx'] = previous_idx if idx > t - \
                        1 else previous_idx + 1
                    if not splits[i - 1] and previous_node.child_count() > previous_idx + 1:
                        value_dict['shifted_idx'] = [
                            i + 1 for i in range(previous_idx + 1, previous_node.child_count())]
                    elif splits[i - 1] and t - 1 > previous_idx:
                        value_dict['shifted_idx'] = [
                            i + 1 for i in range(previous_idx + 1, t)]

                if node_type == 'inner':
                    child_hashes = [
                        node.hash for node in node.children[t: (2 * t)]]
                    value_dict['child_hashes'] = child_hashes
                    path[i] = (node, idx % t)
            else:
                if i == 0:
                    continue
                else:
                    value_dict['updated_idx'] = previous_idx
            update_path.append(value_dict)

        # Part 2: Re-determines the path based on the idexes found in part 1
        self.insert_node(key, value)
        current_node = self.root
        for node in update_path:
            node['updated_node'] = current_node.children[node['updated_idx']]
            if node.get('split_idx') is not None:
                node['split_node'] = current_node.children[node['split_idx']]
            if node.get('shifted_idx') is not None:
                node['shifted_nodes'] = [current_node.children[i]
                                         for i in node['shifted_idx']]
            current_node = node['updated_node']

        update_node_changes = []
        split_node_changes = []

        # Part 3: Update the hashes and commitments of the nodes at each level of the path from the bottom up
        root_dict = {'node_type': 'root', 'updated_node': self.root}
        update_path.insert(0, root_dict)
        for node in reversed(update_path):

            node['updated_node'].node_hash()

            # Calculate changes to nodes on current level
            if node['node_type'] == 'root':
                if update_path[1].get('split_node') is not None:
                    self.add_node_hash(node['updated_node'])
                else:
                    for idx, value_change in update_node_changes:
                        node['updated_node'].commitment.add(
                            self.setup["g1_lagrange"][idx].dup().mult(value_change))
                        node['updated_node'].node_hash()
                return
            if node['node_type'] == 'inner':
                if node.get('split_node') is not None:
                    node['split_node'].node_hash()
                    changes_to_original = [
                        (t + i, (- int_from_bytes(node['child_hashes'][i]) + self.modulus) % self.modulus) for i in range(t)]
                    changes_to_split = [
                        (i, int_from_bytes(node['child_hashes'][i]) % self.modulus) for i in range(t)]
                    if node['updated_idx'] < node['split_idx']:
                        update_node_changes = changes_to_original + update_node_changes
                        split_node_changes = changes_to_split
                    else:
                        update_node_changes = changes_to_split + update_node_changes
                        split_node_changes = changes_to_original

            # Update commits for nodes on current level
            if len(split_node_changes) > 0:
                for idx, value_change in split_node_changes:
                    node['split_node'].commitment.add(
                        self.setup["g1_lagrange"][idx].dup().mult(value_change))
                    node['split_node'].node_hash()
                split_node_changes = []

            if len(update_node_changes) > 0:
                for idx, value_change in update_node_changes:
                    node['updated_node'].commitment.add(
                        self.setup["g1_lagrange"][idx].dup().mult(value_change))
                    node['updated_node'].node_hash()
                update_node_changes = []

            # Calculate changes to nodes on next level
            if node.get('split_node') is not None:
                node['split_node'].node_hash()
                min_idx = min(node['updated_idx'], node['split_idx'])
                nodes = (node['updated_node'], node['split_node']) if node['updated_idx'] < node['split_idx'] else (
                    node['split_node'], node['updated_node'])
                change_to_original = (int_from_bytes(
                    nodes[0].hash) - int_from_bytes(node['hash']) + self.modulus) % self.modulus
                change_to_split = int_from_bytes(nodes[1].hash) % self.modulus

                update_node_changes.append((min_idx, change_to_original))
                update_node_changes.append((min_idx + 1, change_to_split))

                if node.get('shifted_nodes') is not None:
                    for i in range(len(node['shifted_nodes'])):
                        shifted_hash = node['shifted_nodes'][i].hash
                        change_remove_hash = (- int_from_bytes(shifted_hash) +
                                              self.modulus) % self.modulus
                        change_add_hash = int_from_bytes(
                            shifted_hash) % self.modulus
                        update_node_changes.append(
                            (node['shifted_idx'][i] - 1, change_remove_hash))
                        update_node_changes.append(
                            (node['shifted_idx'][i], change_add_hash))
            else:
                update_change = (int_from_bytes(
                    node['updated_node'].hash) - int_from_bytes(node['hash']) + self.modulus) % self.modulus
                update_node_changes.append(
                    (node['updated_idx'], update_change))

    def find_node(self, node: VBTreeNode, key: bytes):
        """
        Search for a node in the tree with key
        """

        key_count = node.key_count()

        while node is not None:
            i = 0
            while i < key_count and key > node.keys[i]:
                i += 1
            if i < key_count and key == node.keys[i]:
                return (node, i)
            elif node.is_leaf():
                break
            else:
                return self.find_node(node.children[i], key)

        return None

    def find_path_to_node(self, node: VBTreeNode, key: bytes, path: list = None) -> list:
        """
        Returns the path from node to the node with key
        """

        key_count = node.key_count()

        if path is None:
            path = []

        while node is not None:
            i = 0
            while i < key_count and key > node.keys[i]:
                i += 1
            path.append((node, i))
            if i < key_count and key == node.keys[i]:
                break
            elif node.is_leaf():
                break
            else:
                return self.find_path_to_node(node.children[i], key, path)
        return path

    def add_node_hash(self, node: VBTreeNode):
        """
        Adds node hashes and commitments recursively down the tree
        """
        if node.is_leaf():
            node.node_hash()
        else:
            values = {}
            nodes = node.children
            for i in range(len(nodes)):

                if nodes[i].hash is None:
                    self.add_node_hash(nodes[i])
                values[i] = int_from_bytes(nodes[i].hash)
            commitment = self.kzg.compute_commitment_lagrange(values)
            node.commitment = commitment
            node.node_hash()

    def check_valid_tree(self, node: VBTreeNode):
        """
        Check if the hashes and commitments are valid down the tree
        """

        if node.is_leaf():
            assert node.hash == hash(node.keys + node.values)
        else:
            values = {}
            nodes = node.children
            for i in range(len(nodes)):

                if nodes[i].hash is None:
                    self.add_node_hash(nodes[i])
                values[i] = int_from_bytes(nodes[i].hash)
                self.check_valid_tree(nodes[i])
            commitment = self.kzg.compute_commitment_lagrange(values)
            assert node.commitment.is_equal(commitment)
            assert node.hash == hash(
                [node.commitment.compress()] + node.keys + node.values)

    def tree_structure(self, node, level: int = 0, prefix: str = "Root", child_idx=None, structure: list = None):
        """
        Returns the tree structure as a list of dictionaries
        """

        if structure is None:
            structure = []

        if node is not None:
            info = {"position": " " * level * 2 + prefix + str(level),
                    "keys": [int_from_bytes(key) for key in node.keys],
                    "values": [int_from_bytes(value) for value in node.values],
                    "child_index": child_idx}
            structure.append(info)
            for i in range(node.child_count()):
                self.tree_structure(
                    node.children[i], level + 1, f"L{i}", i, structure)

        return structure

    def print_path(self, path):
        """
        Prints the path
        """
        for node, idx in path:
            print(node, [(int_from_bytes(key), int_from_bytes(value))
                  for key, value in zip(node.keys, node.value)], idx)

if __name__ == "__main__":
    # Parameters
    MODULUS = 0x73eda753299d7d483339d80809a1d80553bda402fffe5bfeffffffff00000001
    WIDTH_BITS = 2
    WIDTH = 2**WIDTH_BITS
    PRIMITIVE_ROOT = 7
    SECRET = 8927347823478352432985

    # Number of keys to insert, delete, and add
    NUMBER_INITIAL_KEYS = 2**13
    NUMBER_ADDED_KEYS = 2**7
    NUMBER_SEARCH_KEYS = 0
    KEY_RANGE = 2**256-1

    if len(sys.argv) > 1:
        WIDTH_BITS = int(sys.argv[1])
        WIDTH = 2 ** WIDTH_BITS

        KEY_RANGE = 2 ** int(sys.argv[2])
        NUMBER_INITIAL_KEYS = 2 ** int(sys.argv[3])
        NUMBER_ADDED_KEYS = 2 ** int(sys.argv[4]) if int(sys.argv[4]) != 0 else 0
        NUMBER_SEARCH_KEYS = 2 ** int(sys.argv[5]) if int(sys.argv[5]) != 0 else 0

    # Generate setup
    kzg_integration = KzgIntegration(SECRET, MODULUS, WIDTH, PRIMITIVE_ROOT)

    # Generate tree
    root_val, root_value = randint(0, KEY_RANGE), randint(0, KEY_RANGE)
    root = VBTreeNode([int_to_bytes(root_val)], [int_to_bytes(root_value)])
    vb_tree = VBTree(kzg_integration, root)

    # Insert nodes
    values = {}

    time_a = time()
    for i in range(NUMBER_INITIAL_KEYS):
        key, value = randint(0, KEY_RANGE), randint(0, KEY_RANGE)
        vb_tree.insert_node(int_to_bytes(key), int_to_bytes(value))
        values[key] = value
    time_b = time()

    time_initial = time_b - time_a
    print("Inserted {0} elements in {1:.3f} s".format(NUMBER_INITIAL_KEYS, time_initial), file=sys.stderr)

    time_a = time()
    vb_tree.add_node_hash(vb_tree.root)
    time_b = time()
    compute_root = time_b - time_a

    print("Computed VB-tree root in {0:.3f} s".format(compute_root), file=sys.stderr)

    # time_a = time()
    # vb_tree.check_valid_tree(vb_tree.root)
    # time_b = time()
    # compute_tree_valid = time_b - time_a

    # print("[Checked tree valid: {0:.3f} s]".format(compute_tree_valid), file=sys.stderr)

    time_to_add = None
    check_valid_tree_after_add = None
    if NUMBER_ADDED_KEYS > 0:

        time_x = time()
        for i in range(NUMBER_ADDED_KEYS):
            key, value = randint(0, KEY_RANGE), randint(0, KEY_RANGE)
            vb_tree.upsert_vc_node(int_to_bytes(key), int_to_bytes(value))
            values[key] = value
        time_y = time()

        time_to_add = time_y - time_x
        print("Additionally inserted {0} elements in {1:.3f} s".format(NUMBER_ADDED_KEYS, time_to_add), file=sys.stderr)


        time_a = time()
        vb_tree.check_valid_tree(vb_tree.root)
        time_b = time()
        check_valid_tree_after_add = time_b - time_a

        print("[Checked tree valid: {0:.3f} s]".format(check_valid_tree_after_add), file=sys.stderr)


    time_to_search = None
    if NUMBER_SEARCH_KEYS > 0:
        all_keys = list(values.keys())
        shuffle(all_keys)

        keys_to_search = all_keys[:NUMBER_SEARCH_KEYS]

        time_a = time()
        for key in keys_to_search:
            assert vb_tree.find_node(vb_tree.root, int_to_bytes(key)) is not None
        time_b = time()

        time_to_search = time_b - time_a
        print("Searched for {0} elements in {1:.3f} s".format(NUMBER_SEARCH_KEYS, time_to_search), file=sys.stderr)

    if len(sys.argv) > 1:
        print("{0}\t{1}\t{2}\t{3}\t{4}\t{5}\t{6}\t{7}\t{8}\t{9}\t{10}\t{11}".format(
            'VBTree', WIDTH_BITS, WIDTH, KEY_RANGE, NUMBER_INITIAL_KEYS, NUMBER_ADDED_KEYS, 
            time_initial, compute_root,
            time_to_add if time_to_add is not None else '',
            check_valid_tree_after_add if check_valid_tree_after_add is not None else '',
            NUMBER_SEARCH_KEYS if NUMBER_SEARCH_KEYS != 0 else '',
            time_to_search if time_to_search is not None else ''
        ))
