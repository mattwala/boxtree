from __future__ import division, absolute_import, print_function

__copyright__ = "Copyright (C) 2013 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from six.moves import range

import numpy as np
import numpy.linalg as la
import pyopencl as cl

import pytest
from pyopencl.tools import (  # noqa
        pytest_generate_tests_for_pyopencl as pytest_generate_tests)

from boxtree.tools import (
        make_normal_particle_array as p_normal,
        make_surface_particle_array as p_surface,
        make_uniform_particle_array as p_uniform,
        particle_array_to_host)

import logging
logger = logging.getLogger(__name__)


# {{{ fmm interaction completeness test

class ConstantOneExpansionWrangler(object):
    """This implements the 'analytical routines' for a Green's function that is
    constant 1 everywhere. For 'charges' of 'ones', this should get every particle
    a copy of the particle count.
    """

    def __init__(self, tree):
        self.tree = tree

    def multipole_expansion_zeros(self):
        return np.zeros(self.tree.nboxes, dtype=np.float64)

    local_expansion_zeros = multipole_expansion_zeros

    def potential_zeros(self):
        return np.zeros(self.tree.ntargets, dtype=np.float64)

    def _get_source_slice(self, ibox):
        pstart = self.tree.box_source_starts[ibox]
        return slice(
                pstart, pstart + self.tree.box_source_counts_nonchild[ibox])

    def _get_target_slice(self, ibox):
        pstart = self.tree.box_target_starts[ibox]
        return slice(
                pstart, pstart + self.tree.box_target_counts_nonchild[ibox])

    def reorder_sources(self, source_array):
        return source_array[self.tree.user_source_ids]

    def reorder_potentials(self, potentials):
        return potentials[self.tree.sorted_target_ids]

    def form_multipoles(self, level_start_source_box_nrs, source_boxes, src_weights):
        mpoles = self.multipole_expansion_zeros()
        for ibox in source_boxes:
            pslice = self._get_source_slice(ibox)
            mpoles[ibox] += np.sum(src_weights[pslice])

        return mpoles

    def coarsen_multipoles(self, level_start_source_parent_box_nrs,
            source_parent_boxes, mpoles):
        tree = self.tree

        # 2 is the last relevant source_level.
        # 1 is the last relevant target_level.
        # (Nobody needs a multipole on level 0, i.e. for the root box.)
        for source_level in range(tree.nlevels-1, 1, -1):
            start, stop = level_start_source_parent_box_nrs[
                            source_level:source_level+2]
            for ibox in source_parent_boxes[start:stop]:
                for child in tree.box_child_ids[:, ibox]:
                    if child:
                        mpoles[ibox] += mpoles[child]

    def eval_direct(self, target_boxes, neighbor_sources_starts,
            neighbor_sources_lists, src_weights):
        pot = self.potential_zeros()

        for itgt_box, tgt_ibox in enumerate(target_boxes):
            tgt_pslice = self._get_target_slice(tgt_ibox)

            src_sum = 0
            start, end = neighbor_sources_starts[itgt_box:itgt_box+2]
            #print "DIR: %s <- %s" % (tgt_ibox, neighbor_sources_lists[start:end])
            for src_ibox in neighbor_sources_lists[start:end]:
                src_pslice = self._get_source_slice(src_ibox)

                src_sum += np.sum(src_weights[src_pslice])

            pot[tgt_pslice] = src_sum

        return pot

    def multipole_to_local(self,
            level_start_target_or_target_parent_box_nrs,
            target_or_target_parent_boxes,
            starts, lists, mpole_exps):
        local_exps = self.local_expansion_zeros()

        for itgt_box, tgt_ibox in enumerate(target_or_target_parent_boxes):
            start, end = starts[itgt_box:itgt_box+2]

            contrib = 0
            #print tgt_ibox, "<-", lists[start:end]
            for src_ibox in lists[start:end]:
                contrib += mpole_exps[src_ibox]

            local_exps[tgt_ibox] += contrib

        return local_exps

    def eval_multipoles(self, level_start_target_box_nrs, target_boxes,
            sep_smaller_nonsiblings_starts,
            sep_smaller_nonsiblings_lists, mpole_exps):
        pot = self.potential_zeros()

        for itgt_box, tgt_ibox in enumerate(target_boxes):
            tgt_pslice = self._get_target_slice(tgt_ibox)

            contrib = 0
            start, end = sep_smaller_nonsiblings_starts[itgt_box:itgt_box+2]
            for src_ibox in sep_smaller_nonsiblings_lists[start:end]:
                contrib += mpole_exps[src_ibox]

            pot[tgt_pslice] += contrib

        return pot

    def form_locals(self,
            level_start_target_or_target_parent_box_nrs,
            target_or_target_parent_boxes, starts, lists, src_weights):
        local_exps = self.local_expansion_zeros()

        for itgt_box, tgt_ibox in enumerate(target_or_target_parent_boxes):
            start, end = starts[itgt_box:itgt_box+2]

            #print "LIST 4", tgt_ibox, "<-", lists[start:end]
            contrib = 0
            for src_ibox in lists[start:end]:
                src_pslice = self._get_source_slice(src_ibox)

                contrib += np.sum(src_weights[src_pslice])

            local_exps[tgt_ibox] += contrib

        return local_exps

    def refine_locals(self, level_start_target_or_target_parent_box_nrs,
            target_or_target_parent_boxes, local_exps):

        for target_lev in range(1, self.tree.nlevels):
            start, stop = level_start_target_or_target_parent_box_nrs[
                    target_lev:target_lev+2]
            for ibox in target_or_target_parent_boxes[start:stop]:
                local_exps[ibox] += local_exps[self.tree.box_parent_ids[ibox]]

        return local_exps

    def eval_locals(self, level_start_target_box_nrs, target_boxes, local_exps):
        pot = self.potential_zeros()

        for ibox in target_boxes:
            tgt_pslice = self._get_target_slice(ibox)
            pot[tgt_pslice] += local_exps[ibox]

        return pot


class ConstantOneExpansionWranglerWithFilteredTargetsInTreeOrder(
        ConstantOneExpansionWrangler):
    def __init__(self, tree, filtered_targets):
        ConstantOneExpansionWrangler.__init__(self, tree)
        self.filtered_targets = filtered_targets

    def potential_zeros(self):
        return np.zeros(self.filtered_targets.nfiltered_targets, dtype=np.float64)

    def _get_target_slice(self, ibox):
        pstart = self.filtered_targets.box_target_starts[ibox]
        return slice(
                pstart, pstart
                + self.filtered_targets.box_target_counts_nonchild[ibox])

    def reorder_potentials(self, potentials):
        tree_order_all_potentials = np.zeros(self.tree.ntargets, potentials.dtype)
        tree_order_all_potentials[
                self.filtered_targets.unfiltered_from_filtered_target_indices] \
                = potentials

        return tree_order_all_potentials[self.tree.sorted_target_ids]


class ConstantOneExpansionWranglerWithFilteredTargetsInUserOrder(
        ConstantOneExpansionWrangler):
    def __init__(self, tree, filtered_targets):
        ConstantOneExpansionWrangler.__init__(self, tree)
        self.filtered_targets = filtered_targets

    def _get_target_slice(self, ibox):
        user_target_ids = self.filtered_targets.target_lists[
                self.filtered_targets.target_starts[ibox]:
                self.filtered_targets.target_starts[ibox+1]]
        return self.tree.sorted_target_ids[user_target_ids]


@pytest.mark.parametrize(("dims", "nsources_req", "ntargets_req",
        "who_has_extent", "source_gen", "target_gen", "filter_kind"),
        [
            (2, 10**5, None, "", p_normal, p_normal, None),
            (3, 5 * 10**4, 4*10**4, "", p_normal, p_normal, None),
            (2, 5 * 10**5, 4*10**4, "s", p_normal, p_normal, None),
            (2, 5 * 10**5, 4*10**4, "st", p_normal, p_normal, None),
            (2, 5 * 10**5, 4*10**4, "t", p_normal, p_normal, None),
            (2, 5 * 10**5, 4*10**4, "st", p_surface, p_uniform, None),

            (3, 10**5, None, "", p_normal, p_normal, None),
            (3, 5 * 10**4, 4*10**4, "", p_normal, p_normal, None),
            (3, 5 * 10**5, 4*10**4, "s", p_normal, p_normal, None),
            (3, 5 * 10**5, 4*10**4, "st", p_normal, p_normal, None),
            (3, 5 * 10**5, 4*10**4, "t", p_normal, p_normal, None),
            (3, 5 * 10**5, 4*10**4, "st", p_surface, p_uniform, None),

            (2, 10**5, None, "", p_normal, p_normal, "user"),
            (3, 5 * 10**4, 4*10**4, "", p_normal, p_normal, "user"),
            (2, 5 * 10**5, 4*10**4, "s", p_normal, p_normal, "user"),
            (2, 5 * 10**5, 4*10**4, "st", p_normal, p_normal, "user"),
            (2, 5 * 10**5, 4*10**4, "t", p_normal, p_normal, "user"),
            (2, 5 * 10**5, 4*10**4, "st", p_surface, p_uniform, "user"),

            (2, 10**5, None, "", p_normal, p_normal, "tree"),
            (3, 5 * 10**4, 4*10**4, "", p_normal, p_normal, "tree"),
            (2, 5 * 10**5, 4*10**4, "s", p_normal, p_normal, "tree"),
            (2, 5 * 10**5, 4*10**4, "st", p_normal, p_normal, "tree"),
            (2, 5 * 10**5, 4*10**4, "t", p_normal, p_normal, "tree"),
            (2, 5 * 10**5, 4*10**4, "st", p_surface, p_uniform, "tree"),
            ])
def test_fmm_completeness(ctx_getter, dims, nsources_req, ntargets_req,
         who_has_extent, source_gen, target_gen, filter_kind):
    """Tests whether the built FMM traversal structures and driver completely
    capture all interactions.
    """

    sources_have_extent = "s" in who_has_extent
    targets_have_extent = "t" in who_has_extent

    logging.basicConfig(level=logging.INFO)

    ctx = ctx_getter()
    queue = cl.CommandQueue(ctx)

    dtype = np.float64

    try:
        sources = source_gen(queue, nsources_req, dims, dtype, seed=15)
        nsources = len(sources[0])

        if ntargets_req is None:
            # This says "same as sources" to the tree builder.
            targets = None
            ntargets = ntargets_req
        else:
            targets = target_gen(queue, ntargets_req, dims, dtype, seed=16)
            ntargets = len(targets[0])
    except ImportError:
        pytest.skip("loo.py not available, but needed for particle array "
                "generation")

    from pyopencl.clrandom import PhiloxGenerator
    rng = PhiloxGenerator(queue.context, seed=12)
    if sources_have_extent:
        source_radii = 2**rng.uniform(queue, nsources, dtype=dtype,
                a=-10, b=0)
    else:
        source_radii = None

    if targets_have_extent:
        target_radii = 2**rng.uniform(queue, ntargets, dtype=dtype,
                a=-10, b=0)
    else:
        target_radii = None

    from boxtree import TreeBuilder
    tb = TreeBuilder(ctx)

    tree, _ = tb(queue, sources, targets=targets,
            max_particles_in_box=30,
            source_radii=source_radii, target_radii=target_radii,
            debug=True)
    if 0:
        tree.get().plot()
        import matplotlib.pyplot as pt
        pt.show()

    from boxtree.traversal import FMMTraversalBuilder
    tbuild = FMMTraversalBuilder(ctx)
    trav, _ = tbuild(queue, tree, debug=True)
    if trav.sep_close_smaller_starts is not None:
        trav = trav.merge_close_lists(queue)

    weights = np.random.randn(nsources)
    #weights = np.ones(nsources)
    weights_sum = np.sum(weights)

    host_trav = trav.get(queue=queue)
    host_tree = host_trav.tree

    if filter_kind:
        flags = rng.uniform(queue, ntargets or nsources, np.int32, a=0, b=2) \
                .astype(np.int8)
        if filter_kind == "user":
            from boxtree.tree import filter_target_lists_in_user_order
            filtered_targets = filter_target_lists_in_user_order(queue, tree, flags)
            wrangler = ConstantOneExpansionWranglerWithFilteredTargetsInUserOrder(
                    host_tree, filtered_targets.get(queue=queue))
        elif filter_kind == "tree":
            from boxtree.tree import filter_target_lists_in_tree_order
            filtered_targets = filter_target_lists_in_tree_order(queue, tree, flags)
            wrangler = ConstantOneExpansionWranglerWithFilteredTargetsInTreeOrder(
                    host_tree, filtered_targets.get(queue=queue))
        else:
            raise ValueError("unsupported value of 'filter_kind'")
    else:
        wrangler = ConstantOneExpansionWrangler(host_tree)

    if ntargets is None and not filter_kind:
        # This check only works for targets == sources.
        assert (wrangler.reorder_potentials(
                wrangler.reorder_sources(weights)) == weights).all()

    from boxtree.fmm import drive_fmm
    pot = drive_fmm(host_trav, wrangler, weights)

    # {{{ build, evaluate matrix (and identify missing interactions)

    if 0:
        mat = np.zeros((ntargets, nsources), dtype)
        from pytools import ProgressBar

        logging.getLogger().setLevel(logging.WARNING)

        pb = ProgressBar("matrix", nsources)
        for i in range(nsources):
            unit_vec = np.zeros(nsources, dtype=dtype)
            unit_vec[i] = 1
            mat[:, i] = drive_fmm(host_trav, wrangler, unit_vec)
            pb.progress()
        pb.finished()

        logging.getLogger().setLevel(logging.INFO)

        import matplotlib.pyplot as pt

        if 1:
            pt.spy(mat)
            pt.show()

        missing_tgts, missing_srcs = np.where(mat == 0)

        if 1 and len(missing_tgts):
            from boxtree.visualization import TreePlotter
            plotter = TreePlotter(host_tree)
            plotter.draw_tree(fill=False, edgecolor="black")
            plotter.draw_box_numbers()
            plotter.set_bounding_box()

            tree_order_missing_tgts = \
                    host_tree.indices_to_tree_target_order(missing_tgts)
            tree_order_missing_srcs = \
                    host_tree.indices_to_tree_source_order(missing_srcs)

            src_boxes = [
                    host_tree.find_box_nr_for_source(i)
                    for i in tree_order_missing_srcs]
            tgt_boxes = [
                    host_tree.find_box_nr_for_target(i)
                    for i in tree_order_missing_tgts]
            print(src_boxes)
            print(tgt_boxes)

            pt.plot(
                    host_tree.targets[0][tree_order_missing_tgts],
                    host_tree.targets[1][tree_order_missing_tgts],
                    "rv")
            pt.plot(
                    host_tree.sources[0][tree_order_missing_srcs],
                    host_tree.sources[1][tree_order_missing_srcs],
                    "go")
            pt.gca().set_aspect("equal")

            pt.show()

    # }}}

    if filter_kind:
        pot = pot[flags.get() > 0]

    rel_err = la.norm((pot - weights_sum) / nsources)
    good = rel_err < 1e-8
    if 0 and not good:
        import matplotlib.pyplot as pt
        pt.plot(pot-weights_sum)
        pt.show()

    if 0 and not good:
        import matplotlib.pyplot as pt
        filt_targets = [
                host_tree.targets[0][flags.get() > 0],
                host_tree.targets[1][flags.get() > 0],
                ]
        host_tree.plot()
        bad = np.abs(pot - weights_sum) >= 1e-3
        bad_targets = [
                filt_targets[0][bad],
                filt_targets[1][bad],
                ]
        print(bad_targets[0].shape)
        pt.plot(filt_targets[0], filt_targets[1], "x")
        pt.plot(bad_targets[0], bad_targets[1], "v")
        pt.show()

    assert good

# }}}


# {{{ test Helmholtz fmm with pyfmmlib

def test_pyfmmlib_fmm(ctx_getter):
    logging.basicConfig(level=logging.INFO)

    from pytest import importorskip
    importorskip("pyfmmlib")

    ctx = ctx_getter()
    queue = cl.CommandQueue(ctx)

    nsources = 3000
    ntargets = 1000
    dims = 2
    dtype = np.float64

    helmholtz_k = 2

    sources = p_normal(queue, nsources, dims, dtype, seed=15)
    targets = (
            p_normal(queue, ntargets, dims, dtype, seed=18)
            + np.array([2, 0]))

    sources_host = particle_array_to_host(sources)
    targets_host = particle_array_to_host(targets)

    from boxtree import TreeBuilder
    tb = TreeBuilder(ctx)

    tree, _ = tb(queue, sources, targets=targets,
            max_particles_in_box=30, debug=True)

    from boxtree.traversal import FMMTraversalBuilder
    tbuild = FMMTraversalBuilder(ctx)
    trav, _ = tbuild(queue, tree, debug=True)

    trav = trav.get(queue=queue)

    from pyopencl.clrandom import PhiloxGenerator
    rng = PhiloxGenerator(queue.context, seed=20)

    weights = rng.uniform(queue, nsources, dtype=np.float64).get()
    #weights = np.ones(nsources)

    logger.info("computing direct (reference) result")

    from pyfmmlib import hpotgrad2dall_vec
    ref_pot, _, _ = hpotgrad2dall_vec(ifgrad=False, ifhess=False,
            sources=sources_host.T, charge=weights,
            targets=targets_host.T, zk=helmholtz_k)

    from boxtree.pyfmmlib_integration import Helmholtz2DExpansionWrangler
    wrangler = Helmholtz2DExpansionWrangler(trav.tree, helmholtz_k, nterms=10)

    from boxtree.fmm import drive_fmm
    pot = drive_fmm(trav, wrangler, weights)

    rel_err = la.norm(pot - ref_pot) / la.norm(ref_pot)
    logger.info("relative l2 error: %g" % rel_err)
    assert rel_err < 1e-5

# }}}


# You can test individual routines by typing
# $ python test_fmm.py 'test_routine(cl.create_some_context)'

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        exec(sys.argv[1])
    else:
        from py.test.cmdline import main
        main([__file__])

# vim: fdm=marker
