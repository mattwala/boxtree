from __future__ import division

"""Integration between boxtree and pyfmmlib."""

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


import numpy as np


__doc__ = """Integrates :mod:`boxtree` with
`pyfmmlib <http://pypi.python.org/pypi/pyfmmlib>`_.
"""


class Helmholtz2DExpansionWrangler:
    """Implements the :class:`boxtree.fmm.ExpansionWranglerInterface`
    by using pyfmmlib.
    """

    def __init__(self, tree, helmholtz_k, nterms):
        self.tree = tree
        self.helmholtz_k = helmholtz_k
        self.nterms = nterms

    def multipole_expansion_zeros(self):
        return np.zeros((self.tree.nboxes, 2*self.nterms+1), dtype=np.complex128)

    local_expansion_zeros = multipole_expansion_zeros

    def potential_zeros(self):
        return np.zeros(self.tree.ntargets, dtype=np.complex128)

    def _get_source_slice(self, ibox):
        pstart = self.tree.box_source_starts[ibox]
        return slice(
                pstart, pstart + self.tree.box_source_counts_nonchild[ibox])

    def _get_target_slice(self, ibox):
        pstart = self.tree.box_target_starts[ibox]
        return slice(
                pstart, pstart + self.tree.box_target_counts_nonchild[ibox])

    def _get_sources(self, pslice):
        # FIXME yuck!
        return np.array([
            self.tree.sources[idim][pslice]
            for idim in range(self.tree.dimensions)
            ], order="F")

    def _get_targets(self, pslice):
        # FIXME yuck!
        return np.array([
            self.tree.targets[idim][pslice]
            for idim in range(self.tree.dimensions)
            ], order="F")

    def reorder_sources(self, source_array):
        return source_array[self.tree.user_source_ids]

    def reorder_potentials(self, potentials):
        return potentials[self.tree.sorted_target_ids]

    def form_multipoles(self, level_start_source_box_nrs, source_boxes, src_weights):
        rscale = 1  # FIXME

        from pyfmmlib import h2dformmp

        mpoles = self.multipole_expansion_zeros()
        for src_ibox in source_boxes:
            pslice = self._get_source_slice(src_ibox)

            if pslice.stop - pslice.start == 0:
                continue

            ier, mpoles[src_ibox] = h2dformmp(
                    self.helmholtz_k, rscale, self._get_sources(pslice),
                    src_weights[pslice],
                    self.tree.box_centers[:, src_ibox], self.nterms)
            if ier:
                raise RuntimeError("h2dformmp failed")

        return mpoles

    def coarsen_multipoles(self, level_start_source_parent_box_nrs,
            source_parent_boxes, mpoles):
        tree = self.tree
        rscale = 1  # FIXME

        from pyfmmlib import h2dmpmp_vec

        # 2 is the last relevant source_level.
        # 1 is the last relevant target_level.
        # (Nobody needs a multipole on level 0, i.e. for the root box.)
        for source_level in range(tree.nlevels-1, 1, -1):
            start, stop = level_start_source_parent_box_nrs[
                            source_level:source_level+2]
            for ibox in source_parent_boxes[start:stop]:
                parent_center = tree.box_centers[:, ibox]
                for child in tree.box_child_ids[:, ibox]:
                    if child:
                        child_center = tree.box_centers[:, child]

                        new_mp = h2dmpmp_vec(
                                self.helmholtz_k,
                                rscale, child_center, mpoles[child],
                                rscale, parent_center, self.nterms)

                        mpoles[ibox] += new_mp[:, 0]

    def eval_direct(self, target_boxes, neighbor_sources_starts,
            neighbor_sources_lists, src_weights):
        pot = self.potential_zeros()

        from pyfmmlib import hpotgrad2dall_vec

        for itgt_box, tgt_ibox in enumerate(target_boxes):
            tgt_pslice = self._get_target_slice(tgt_ibox)

            if tgt_pslice.stop - tgt_pslice.start == 0:
                continue

            tgt_result = np.zeros(tgt_pslice.stop - tgt_pslice.start, np.complex128)
            start, end = neighbor_sources_starts[itgt_box:itgt_box+2]
            for src_ibox in neighbor_sources_lists[start:end]:
                src_pslice = self._get_source_slice(src_ibox)

                if src_pslice.stop - src_pslice.start == 0:
                    continue

                tmp_pot, _, _ = hpotgrad2dall_vec(
                        ifgrad=False, ifhess=False,
                        sources=self._get_sources(src_pslice),
                        charge=src_weights[src_pslice],
                        targets=self._get_targets(tgt_pslice), zk=self.helmholtz_k)

                tgt_result += tmp_pot

            pot[tgt_pslice] = tgt_result

        return pot

    def multipole_to_local(self,
            level_start_target_or_target_parent_box_nrs,
            target_or_target_parent_boxes,
            starts, lists, mpole_exps):
        tree = self.tree
        local_exps = self.local_expansion_zeros()

        rscale = 1

        from pyfmmlib import h2dmploc_vec

        for itgt_box, tgt_ibox in enumerate(target_or_target_parent_boxes):
            start, end = starts[itgt_box:itgt_box+2]
            tgt_center = tree.box_centers[:, tgt_ibox]

            #print tgt_ibox, "<-", lists[start:end]
            tgt_loc = 0

            for src_ibox in lists[start:end]:
                src_center = tree.box_centers[:, src_ibox]

                tgt_loc = tgt_loc + h2dmploc_vec(
                        self.helmholtz_k,
                        rscale, src_center, mpole_exps[src_ibox],
                        rscale, tgt_center, self.nterms)[:, 0]

            local_exps[tgt_ibox] += tgt_loc

        return local_exps

    def eval_multipoles(self, level_start_target_box_nrs, target_boxes,
            starts, lists, mpole_exps):
        pot = self.potential_zeros()

        rscale = 1

        from pyfmmlib import h2dmpeval_vec
        for itgt_box, tgt_ibox in enumerate(target_boxes):
            tgt_pslice = self._get_target_slice(tgt_ibox)

            if tgt_pslice.stop - tgt_pslice.start == 0:
                continue

            tgt_pot = 0
            start, end = starts[itgt_box:itgt_box+2]
            for src_ibox in lists[start:end]:

                tmp_pot, _, _ = h2dmpeval_vec(self.helmholtz_k, rscale, self.
                        tree.box_centers[:, src_ibox], mpole_exps[src_ibox],
                        self._get_targets(tgt_pslice),
                        ifgrad=False, ifhess=False)

                tgt_pot = tgt_pot + tmp_pot

            pot[tgt_pslice] += tgt_pot

        return pot

    def form_locals(self,
            level_start_target_or_target_parent_box_nrs,
            target_or_target_parent_boxes, starts, lists, src_weights):
        rscale = 1  # FIXME
        local_exps = self.local_expansion_zeros()

        from pyfmmlib import h2dformta

        for itgt_box, tgt_ibox in enumerate(target_or_target_parent_boxes):
            start, end = starts[itgt_box:itgt_box+2]

            contrib = 0

            for src_ibox in lists[start:end]:
                src_pslice = self._get_source_slice(src_ibox)
                tgt_center = self.tree.box_centers[:, tgt_ibox]

                if src_pslice.stop - src_pslice.start == 0:
                    continue

                ier, mpole = h2dformta(
                        self.helmholtz_k, rscale,
                        self._get_sources(src_pslice), src_weights[src_pslice],
                        tgt_center, self.nterms)
                if ier:
                    raise RuntimeError("h2dformta failed")

                contrib = contrib + mpole

            local_exps[tgt_ibox] = contrib

        return local_exps

    def refine_locals(self, level_start_target_or_target_parent_box_nrs,
            target_or_target_parent_boxes, local_exps):
        rscale = 1  # FIXME

        from pyfmmlib import h2dlocloc_vec

        for target_lev in range(1, self.tree.nlevels):
            start, stop = level_start_target_or_target_parent_box_nrs[
                    target_lev:target_lev+2]

            for tgt_ibox in target_or_target_parent_boxes[start:stop]:
                tgt_center = self.tree.box_centers[:, tgt_ibox]
                src_ibox = self.tree.box_parent_ids[tgt_ibox]
                src_center = self.tree.box_centers[:, src_ibox]

                tmp_loc_exp = h2dlocloc_vec(
                            self.helmholtz_k,
                            rscale, src_center, local_exps[src_ibox],
                            rscale, tgt_center, self.nterms)[:, 0]

                local_exps[tgt_ibox] += tmp_loc_exp

        return local_exps

    def eval_locals(self, level_start_target_box_nrs, target_boxes, local_exps):
        pot = self.potential_zeros()
        rscale = 1  # FIXME

        from pyfmmlib import h2dtaeval_vec

        for tgt_ibox in target_boxes:
            tgt_pslice = self._get_target_slice(tgt_ibox)

            if tgt_pslice.stop - tgt_pslice.start == 0:
                continue

            tmp_pot, _, _ = h2dtaeval_vec(self.helmholtz_k, rscale,
                    self.tree.box_centers[:, tgt_ibox], local_exps[tgt_ibox],
                    self._get_targets(tgt_pslice), ifgrad=False, ifhess=False)

            pot[tgt_pslice] += tmp_pot

        return pot
