"""
Performs object lookup for a single attribute in a FrozenHashBox.

=== HOW THIS WORKS ===

There are three numpy arrays of length n_objects:
- Array of object indices
- Array of values
- Array of value hashes (this is actually not stored, only used to compute the arrays below.)

These are stored in a carefully chosen order for fast lookup.
They are sorted by hash, then grouped within each hash by value, and within that they are sorted by object index.

There is a second set of parallel arrays, of shorter length.
These are generated by run-length encoding of the value hashes array above.
 - unique_hashes: contains one entry for each unique hash
 - hash_starts: stores an index for lookup in the three n_objects arrays
 - hash_run_lengths: stores the number of items with the same hash.

On find(val), we do:
 - val_hash = hash(val)
 - Bisect the array of unique hashes to find the matching val_hash. If none, return.
 - Get the start and length of the values matching that hash from hash_starts and hash_run_lengths.
 - Now we know the range of object indices that match on hash -- but not necessarily on value!
 - Initialize two pointers in the array of values at the start and end of the matching hash range.
 - Move pointers inwards until they both match the val.
 - Return the object indices that match the val. Note that they are already sorted.

Why not just sort and bisect on value? Because the values may not be sortable. Imagine
values like [(1, 3), 'kwyjibo', SomeWeirdHashableObject()]. How you gonna sort those?
Defining a comparator between them would be wacky.
But hashes are nice; hash(obj) is just an int in the int64 range. Hashes are definitely comparable, and very likely to
be unique. So they're the best comparator we can hope for. We just need to handle the collisions, as detailed above.

And there's one last optimization in here, to handle a hash collision scenario.
Suppose we had two values, val_big and val_small, with the same hash.
val1 is associated with a million objects; val2 is associated with ten.
When we look up val_small, we don't want to have to crawl through all the val_big values -- that
would take tens of milliseconds! Every query involving val_small would be a performance disaster.
So for values with many objects, like val_big, we extract those off into their own arrays and give them
a dict lookup. It adds a little code complexity and initialization time, but it makes query times predictable.
Further, we don't need to store many copies of val1 in that case -- just one, for the dict lookup. So it saves
memory as well. Super worth it.
"""


import numpy as np

from bisect import bisect_left
from dataclasses import dataclass
from typing import Union, Callable

from hashbox.init_helpers import sort_by_hash, group_by_val, run_length_encode
from hashbox.constants import SIZE_THRESH
from hashbox.utils import make_empty_array


@dataclass
class ObjsByHash:
    sorted_obj_ids: np.ndarray
    sorted_vals: np.ndarray
    unique_hashes: np.ndarray
    hash_starts: np.ndarray
    hash_run_lengths: np.ndarray
    dtype: str

    def get(self, val):
        val_hash = hash(val)
        i = bisect_left(self.unique_hashes, val_hash)
        if i < 0 or i >= len(self.unique_hashes) or self.unique_hashes[i] != val_hash:
            return make_empty_array(self.dtype)
        start = self.hash_starts[i]
        end = self.hash_starts[i] + self.hash_run_lengths[i]
        # Typically the hash will only contain the one val we want.
        # But hash collisions do happen.
        # Shrink the range until it contains only our value.
        while start < end and self.sorted_vals[start] != val:
            start += 1
        while end > start and self.sorted_vals[end - 1] != val:
            end -= 1
        if end == start:
            return make_empty_array(self.dtype)
        return self.sorted_obj_ids[start:end]


class FrozenAttrIndex:
    """Stores data and handles requests that are relevant to a single attribute of a FrozenHashBox."""

    def __init__(self, attr: Union[str, Callable], objs: np.ndarray, dtype: str):
        # sort the objects by attribute value, using their hashes and handling collisions
        self.dtype = dtype
        obj_id_arr = np.arange(len(objs), dtype=self.dtype)
        sorted_hashes, sorted_vals, sorted_obj_ids = sort_by_hash(
            objs, obj_id_arr, attr
        )
        group_by_val(sorted_hashes, sorted_vals, sorted_obj_ids)

        # find runs of the same value, get the start positions and lengths of those runs
        val_starts, val_run_lengths, unique_vals = run_length_encode(sorted_vals)

        # Pre-bake a dict of {val: array_pair} where there are many objs with the same val.
        self.val_to_obj_ids = dict()
        unused = np.ones_like(sorted_obj_ids, dtype="bool")
        n_unused = len(unused)
        for i, val in enumerate(unique_vals):
            if val_run_lengths[i] > SIZE_THRESH:
                # extract these
                start = val_starts[i]
                end = start + val_run_lengths[i]
                unused[start:end] = False
                n_unused -= val_run_lengths[i]
                obj_id_arr = sorted_obj_ids[start:end]
                self.val_to_obj_ids[val] = np.sort(obj_id_arr)

        # Put all remaining objs into one big object array 'objs_by_hash'.
        # During query, use bisection on the hash value to locate objects.
        # The output obj_id_arrays will be made during query.
        if n_unused == 0:
            self.objs_by_hash = None
            return

        if n_unused == len(sorted_obj_ids):
            hash_starts, hash_run_lengths, unique_hashes = run_length_encode(
                sorted_hashes
            )
            self.objs_by_hash = ObjsByHash(
                sorted_obj_ids=sorted_obj_ids,
                sorted_vals=sorted_vals,
                unique_hashes=unique_hashes,
                hash_starts=hash_starts,
                hash_run_lengths=hash_run_lengths,
                dtype=self.dtype,
            )
            return

        # we have a mix of cardinalities
        unused_idx = np.where(unused)
        sorted_obj_ids = sorted_obj_ids[unused_idx]
        sorted_hashes = sorted_hashes[unused_idx]
        sorted_vals = sorted_vals[unused_idx]
        hash_starts, hash_run_lengths, unique_hashes = run_length_encode(sorted_hashes)
        self.objs_by_hash = ObjsByHash(
            sorted_obj_ids=sorted_obj_ids,
            sorted_vals=sorted_vals,
            unique_hashes=unique_hashes,
            hash_starts=hash_starts,
            hash_run_lengths=hash_run_lengths,
            dtype=self.dtype,
        )

    def get(self, val) -> np.ndarray:
        """Get indices of objects whose attribute is val."""
        if val in self.val_to_obj_ids:
            # these are stored in sorted order
            return self.val_to_obj_ids[val]
        elif self.objs_by_hash is not None:
            return np.sort(self.objs_by_hash.get(val))
        else:
            return make_empty_array(self.dtype)

    def get_all(self):
        """Get indices of every object with this attribute. Used when matching ANY."""
        if self.objs_by_hash is None:
            arrs = []
        else:
            arrs = [self.objs_by_hash.sorted_obj_ids]
        for v in self.val_to_obj_ids.values():
            arrs.append(v)
        return np.sort(np.concatenate(arrs))

    def __len__(self):
        tot = sum(len(v) for v in self.val_to_obj_ids.values())
        return tot + len(self.objs_by_hash.sorted_obj_ids)
