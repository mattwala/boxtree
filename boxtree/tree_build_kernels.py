from __future__ import division
from __future__ import absolute_import
import six
from six.moves import range

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
import pyopencl as cl
from pyopencl.elementwise import ElementwiseTemplate
from pyopencl.scan import ScanTemplate
from mako.template import Template
from pytools import Record, memoize
from boxtree.tools import get_type_moniker

import logging
logger = logging.getLogger(__name__)


# TODO:
# - Add *restrict where applicable.

# -----------------------------------------------------------------------------
# CONTROL FLOW
# ------------
#
# Since this file mostly fills in the blanks in the outer parallel 'scan'
# implementation, control flow here can be a bit hard to see.
#
# - Everything starts and ends in the 'driver' bit at the end.
#
# - The first thing that happens is that data types get built and
#   kernels get compiled. Most of the file consists of type and
#   code generators for these kernels.
#
# - We start with a reduction that determines the bounding box of all
#   particles.
#
# - The level loop is in the driver below, which alternates between
#   scans and local post processing ("split and sort"), according to
#   the algorithm described below.
#
# - Once the level loop finishes, a "box info" kernel is run
#   that extracts some more information for each box. (center, level, ...)
#
# - As a last step, empty leaf boxes are eliminated. This is done by a
#   scan kernel that computes indices, and by an elementwise kernel
#   that compresses arrays and maps them to new box IDs, if applicable.
#
# -----------------------------------------------------------------------------
#
# HOW DOES THE PRIMARY SCAN WORK?
# -------------------------------
#
# This code sorts particles into an nD-tree of boxes.  It does this by doing two
# succesive (parallel) scans over particles and a (local, i.e. independent for
# each particle) postprocessing step for each level.
#
# The following information is being pushed around by the scans, which
# proceed over particles:
#
# - a cumulative count ("pcnt") and weight ("pwt") of particles in each subbox
#   ("morton_nr") at the current level, should the current box need to be
#   subdivided.
#
# - the "split_box_id". This is the box number that the particle gets pushed
#   into.  The very first entry here gets initialized to the number of boxes
#   present at the previous level.
#
# Using this data, the stages of the algorithm proceed as follows:
#
# 1. Count the number of particles in each subbox. This stage uses a segmented
#    (per-box) scan to fill "pcnt" and "pwt".
#
# 2. Using a global (non-segmented) scan over the particles, make a decision
#    whether to refine each box, and compute the total number of new boxes
#    needed. This stage also computes the split_box_id for each particle. If a
#    box knows it needs to be subdivided, its first particle asks for 2**d new
#    boxes.
#
# 3. Realize the splitting determined in #2. This stage proceeds in an
#    element-wise fashion over the particles.
#
# -----------------------------------------------------------------------------


class _KernelInfo(Record):
    pass


# {{{ data types

refine_weight_dtype = np.dtype(np.int32)


@memoize
def make_morton_bin_count_type(device, dimensions, particle_id_dtype,
        srcntgts_have_extent):
    fields = []

    # Non-child srcntgts are sorted *before* all the child srcntgts.
    if srcntgts_have_extent:
        fields.append(("nonchild_srcntgts", particle_id_dtype))

    from boxtree.tools import padded_bin
    for mnr in range(2**dimensions):
        fields.append(("pcnt%s" % padded_bin(mnr, dimensions), particle_id_dtype))
    # Morton bin weight totals
    for mnr in range(2**dimensions):
        fields.append(("pwt%s" % padded_bin(mnr, dimensions), refine_weight_dtype))

    dtype = np.dtype(fields)

    name_suffix = ""
    if srcntgts_have_extent:
        name_suffix = "_ext"

    name = "boxtree_morton_bin_count_%dd_p%s%s_t" % (
            dimensions,
            get_type_moniker(particle_id_dtype),
            name_suffix)

    from pyopencl.tools import get_or_register_dtype, match_dtype_to_c_struct
    dtype, c_decl = match_dtype_to_c_struct(device, name, dtype)

    dtype = get_or_register_dtype(name, dtype)
    return dtype, c_decl

# }}}

# {{{ preamble

TYPE_DECL_PREAMBLE_TPL = Template(r"""//CL//
    typedef ${dtype_to_ctype(morton_bin_count_dtype)} morton_counts_t;
    typedef morton_counts_t scan_t;
    typedef ${dtype_to_ctype(refine_weight_dtype)} refine_weight_t;
    typedef ${dtype_to_ctype(bbox_dtype)} bbox_t;
    typedef ${dtype_to_ctype(coord_dtype)} coord_t;
    typedef ${dtype_to_ctype(coord_vec_dtype)} coord_vec_t;
    typedef ${dtype_to_ctype(box_id_dtype)} box_id_t;
    typedef ${dtype_to_ctype(particle_id_dtype)} particle_id_t;

    // morton_nr == -1 is defined to mean that the srcntgt is
    // remaining at the present level and will not be sorted
    // into a child box.
    typedef ${dtype_to_ctype(morton_nr_dtype)} morton_nr_t;
    """, strict_undefined=True)

GENERIC_PREAMBLE_TPL = Template(r"""//CL//
    #define STICK_OUT_FACTOR ((coord_t) ${stick_out_factor})

    // Use this as dbg_printf(("oh snap: %d\n", stuff)); Note the double
    // parentheses.
    //
    // Watch out: 64-bit values on Intel CL must be printed with %ld, or
    // subsequent values will print as 0. Things may crash. And you'll be very
    // confused.

    %if enable_printf:
        #define dbg_printf(ARGS) printf ARGS
    %else:
        #define dbg_printf(ARGS) /* */
    %endif

    %if enable_assert:
        #define dbg_assert(CONDITION) \
            { \
                if (!(CONDITION)) \
                    printf("*** ASSERTION VIOLATED: %s, line %d: %s\n", \
                        __func__, __LINE__, #CONDITION); \
            }
    %else:
        #define dbg_assert(CONDITION) ((void) 0)
    %endif

""", strict_undefined=True)

# }}}

# BEGIN KERNELS IN THE LEVEL LOOP

# {{{ scan primitive code template

SCAN_PREAMBLE_TPL = Template(r"""//CL//

    // {{{ neutral element

    scan_t scan_t_neutral()
    {
        scan_t result;

        %if srcntgts_have_extent:
            result.nonchild_srcntgts = 0;
        %endif

        %for mnr in range(2**dimensions):
            result.pcnt${padded_bin(mnr, dimensions)} = 0;
        %endfor
        %for mnr in range(2**dimensions):
            result.pwt${padded_bin(mnr, dimensions)} = 0;
        %endfor
        return result;
    }

    // }}}

    inline int my_add_sat(int a, int b)
    {
        long result = (long) a + b;
        return (result > INT_MAX) ? INT_MAX : result;
    }

    // {{{ scan 'add' operation
    scan_t scan_t_add(scan_t a, scan_t b, bool across_seg_boundary)
    {
        if (!across_seg_boundary)
        {
            %if srcntgts_have_extent:
                b.nonchild_srcntgts += a.nonchild_srcntgts;
            %endif

            %for mnr in range(2**dimensions):
                <% field = "pcnt"+padded_bin(mnr, dimensions) %>
                b.${field} = a.${field} + b.${field};
            %endfor
            %for mnr in range(2**dimensions):
                <% field = "pwt"+padded_bin(mnr, dimensions) %>
                // XXX: The use of add_sat() seems to be causing trouble
                // with multiple compilers. For d=3:
                // 1. POCL will miscompile and either give wrong
                //    results or crash.
                // 2. Intel will use a large amount of memory.
                // Versions tested: POCL 0.13, Intel OpenCL 16.1
                b.${field} = my_add_sat(a.${field}, b.${field});
            %endfor
        }

        return b;
    }

    // }}}

    // {{{ scan data type init from particle

    scan_t scan_t_from_particle(
        const int i,
        const int level,
        bbox_t const *bbox,
        global morton_nr_t *morton_nrs, // output/side effect
        global particle_id_t *user_srcntgt_ids,
        global refine_weight_t *refine_weights
        %for ax in axis_names:
            , global const coord_t *${ax}
        %endfor
        %if srcntgts_have_extent:
            , global const coord_t *srcntgt_radii
        %endif
    )
    {
        particle_id_t user_srcntgt_id = user_srcntgt_ids[i];

        // Recall that 'level' is the level currently being built, e.g. 1 at
        // the root.  This should be 0.5 at level 1. (Level 0 is the root.)
        coord_t next_level_box_size_factor =
            ((coord_t) 1) / ((coord_t) (1U << level));

        %if srcntgts_have_extent:
            bool stop_srcntgt_descent = false;
            coord_t srcntgt_radius = srcntgt_radii[user_srcntgt_id];
        %endif

        const coord_t one_half = ((coord_t) 1) / 2;
        const coord_t box_radius_factor =
            // AMD CPU seems to like to miscompile this--change with care.
            // (last seen on 13.4-2)
            (1. + STICK_OUT_FACTOR)
            * one_half; // convert diameter to radius

        %for ax in axis_names:
            // Most FMMs are isotropic, i.e. global_extent_{x,y,z} are all the same.
            // Nonetheless, the gain from exploiting this assumption seems so
            // minimal that doing so here didn't seem worthwhile.

            coord_t global_min_${ax} = bbox->min_${ax};
            coord_t global_extent_${ax} = bbox->max_${ax} - global_min_${ax};
            coord_t srcntgt_${ax} = ${ax}[user_srcntgt_id];

            // Note that the upper bound of the global bounding box is computed
            // to be slightly larger than the highest found coordinate, so that
            // 1.0 is never reached as a scaled coordinate at the highest
            // level, and it isn't either by the fact that boxes are
            // [)-half-open in subsequent levels.

            // So (1 << level) is 2 when building level 1.  Because the
            // floating point factor is strictly less than 1, 2 is never
            // reached, so when building level 1, the result is either 0 or 1.
            // After that, we just add one (less significant) bit per level.

            unsigned ${ax}_bits = (unsigned) (
                ((srcntgt_${ax} - global_min_${ax}) / global_extent_${ax})
                * (1U << level));

            %if srcntgts_have_extent:
                // Need to compute center to compare excess with STICK_OUT_FACTOR.
                coord_t next_level_box_center_${ax} =
                    global_min_${ax}
                    + global_extent_${ax}
                    * (${ax}_bits + one_half)
                    * next_level_box_size_factor;

                coord_t next_level_box_stick_out_radius_${ax} =
                    box_radius_factor
                    * global_extent_${ax}
                    * next_level_box_size_factor;

                stop_srcntgt_descent = stop_srcntgt_descent ||
                    (srcntgt_${ax} + srcntgt_radius >=
                        next_level_box_center_${ax}
                        + next_level_box_stick_out_radius_${ax});
                stop_srcntgt_descent = stop_srcntgt_descent ||
                    (srcntgt_${ax} - srcntgt_radius <
                        next_level_box_center_${ax}
                        - next_level_box_stick_out_radius_${ax});
            %endif
        %endfor

        // Pick off the lowest-order bit for each axis, put it in its place.
        int level_morton_number = 0
        %for iax, ax in enumerate(axis_names):
            | (${ax}_bits & 1U) << (${dimensions-1-iax})
        %endfor
            ;

        %if srcntgts_have_extent:
            if (stop_srcntgt_descent)
            {
                level_morton_number = -1;
            }
        %endif

        scan_t result;
        %if srcntgts_have_extent:
            result.nonchild_srcntgts = (level_morton_number == -1);
        %endif
        %for mnr in range(2**dimensions):
            <% field = "pcnt"+padded_bin(mnr, dimensions) %>
            result.${field} = (level_morton_number == ${mnr});
        %endfor
        %for mnr in range(2**dimensions):
            <% field = "pwt"+padded_bin(mnr, dimensions) %>
            result.${field} = (level_morton_number == ${mnr}) ?
                    refine_weights[user_srcntgt_id] : 0;
        %endfor
        morton_nrs[i] = level_morton_number;

        return result;
    }

    // }}}

""", strict_undefined=True)

# }}}

# {{{ scan output code template

SCAN_OUTPUT_STMT_TPL = Template(r"""//CL//
    {
        particle_id_t my_id_in_my_box = -1
        %if srcntgts_have_extent:
            + item.nonchild_srcntgts
        %endif
        %for mnr in range(2**dimensions):
            + item.pcnt${padded_bin(mnr, dimensions)}
        %endfor
            ;
        dbg_printf(("my_id_in_my_box:%d\n", my_id_in_my_box));
        morton_bin_counts[i] = item;

        box_id_t current_box_id = srcntgt_box_ids[i];
        particle_id_t box_srcntgt_count = box_srcntgt_counts_cumul[current_box_id];

        // Am I the last particle in my current box?
        // If so, populate particle count.

        if (my_id_in_my_box+1 == box_srcntgt_count)
        {
            dbg_printf(("store box %d cbi:%d\n", i, current_box_id));
            dbg_printf(("   store_sums: %d %d %d %d\n",
                item.pcnt00, item.pcnt01, item.pcnt10, item.pcnt11));
            box_morton_bin_counts[current_box_id] = item;
        }
    }
""", strict_undefined=True)

# }}}

# {{{ split box id scan

SPLIT_BOX_ID_SCAN_TPL = ScanTemplate(
    arguments=r"""//CL:mako//
        /* input */
        box_id_t *srcntgt_box_ids,
        particle_id_t *box_srcntgt_starts,
        particle_id_t *box_srcntgt_counts_cumul,
        morton_counts_t *box_morton_bin_counts,
        refine_weight_t *refine_weights,
        refine_weight_t max_leaf_refine_weight,
        box_level_t *box_levels,
        box_level_t level,

        /* input/output */
        box_id_t *nboxes,

        /* output */
        int *box_has_children,
        box_id_t *split_box_ids,
        """,
    preamble=r"""//CL:mako//
        scan_t count_new_boxes_needed(
            particle_id_t i,
            box_id_t box_id,
            refine_weight_t max_leaf_refine_weight,
            __global box_id_t *nboxes,
            __global particle_id_t *box_srcntgt_starts,
            __global particle_id_t *box_srcntgt_counts_cumul,
            __global morton_counts_t *box_morton_bin_counts,
            __global box_level_t *box_levels,
            __global int *box_has_children, // output/side effect
            box_level_t level
            )
        {
            scan_t result = 0;

            // First particle? Start counting at (the previous level's) nboxes.
            if (i == 0)
                result += *nboxes;

            particle_id_t first_particle_in_my_box =
                box_srcntgt_starts[box_id];

            %if srcntgts_have_extent:
                const particle_id_t nonchild_srcntgts_in_box =
                    box_morton_bin_counts[box_id].nonchild_srcntgts;
            %else:
                const particle_id_t nonchild_srcntgts_in_box = 0;
            %endif

            // Get box refine weight.
            refine_weight_t box_refine_weight = 0;
            %for mnr in range(2**dimensions):
                box_refine_weight = add_sat(box_refine_weight,
                    box_morton_bin_counts[box_id].pwt${padded_bin(mnr, dimensions)});
            %endfor

            // Add 2**d to make enough room for a split of the current box
            // This will be the split_box_id for *all* particles in this box,
            // including non-child srcntgts.

            if (i == first_particle_in_my_box
                %if srcntgts_have_extent:
                    // Only last-level boxes get to produce new boxes.
                    // If srcntgts have extent, then prior-level boxes
                    // will keep asking for more boxes to be allocated.
                    // Prevent that.
                    &&
                    box_levels[box_id] + 1 == level
                %endif
                &&
                %if adaptive:
                    /* box overfull? */
                    box_refine_weight
                        > max_leaf_refine_weight
                %else:
                    /* box non-empty? */
                    /* Note: Refine weights are allowed to be 0,
                       so check # of particles directly. */
                    box_srcntgt_counts_cumul[box_id] - nonchild_srcntgts_in_box
                        > 0
                %endif
                )
            {
                result += ${2**dimensions};
                box_has_children[box_id] = 1;
            }

            return result;
        }
        """,
    input_expr="""count_new_boxes_needed(
            i, srcntgt_box_ids[i], max_leaf_refine_weight, nboxes,
            box_srcntgt_starts, box_srcntgt_counts_cumul, box_morton_bin_counts,
            box_levels, box_has_children, level
            )""",
    scan_expr="a + b",
    neutral="0",
    output_statement="""//CL//
        dbg_assert(item >= 0);

        split_box_ids[i] = item;

        // Am I the last particle overall? If so, write box count
        if (i+1 == N)
            *nboxes = item;
        """)

# }}}

# {{{ split-and-sort kernel

SPLIT_AND_SORT_PREAMBLE_TPL = Template(r"""//CL//
    <%
      def get_count_for_branch(known_bits):
          if len(known_bits) == dimensions:
              return "counts.pcnt%s" % known_bits

          dim = len(known_bits)
          boundary_morton_nr = known_bits + "1" + (dimensions-dim-1)*"0"

          return ("((morton_nr < %s) ? %s : %s)" % (
              int(boundary_morton_nr, 2),
              get_count_for_branch(known_bits+"0"),
              get_count_for_branch(known_bits+"1")))
    %>

    particle_id_t get_count(morton_counts_t counts, int morton_nr)
    {
        %if srcntgts_have_extent:
            if (morton_nr == -1)
                return counts.nonchild_srcntgts;
        %endif
        return ${get_count_for_branch("")};
    }

""", strict_undefined=True)


SPLIT_AND_SORT_KERNEL_TPL = Template(r"""//CL//
    box_id_t ibox = srcntgt_box_ids[i];
    dbg_assert(ibox >= 0);
    dbg_assert(ibox < nboxes);

    dbg_printf(("postproc %d:\n", i));
    dbg_printf(("   my box id: %d\n", ibox));

    bool do_split_box = box_has_children[ibox];
    %if srcntgts_have_extent:
        ## Only do split-box processing for srcntgts that were touched
        ## on the immediately preceding level.
        ##
        ## If srcntgts have no extent, then subsequent levels
        ## will never decide to split boxes that were kept unsplit on prior
        ## levels either. If srcntgts do
        ## have an extent, this could happen. Prevent running the
        ## split code for such particles.

        int box_level = box_levels[ibox];
        do_split_box = do_split_box && box_level + 1 == level;
    %endif

    if (do_split_box)
    {
        morton_nr_t my_morton_nr = morton_nrs[i];
        dbg_printf(("   my morton nr: %d\n", my_morton_nr));

        morton_counts_t my_box_morton_bin_counts = box_morton_bin_counts[ibox];

        morton_counts_t my_morton_bin_counts = morton_bin_counts[i];
        particle_id_t my_count = get_count(my_morton_bin_counts, my_morton_nr);

        // {{{ compute this srcntgt's new index

        particle_id_t my_box_start = box_srcntgt_starts[ibox];
        particle_id_t tgt_particle_idx = my_box_start + my_count-1;
        %if srcntgts_have_extent:
            tgt_particle_idx +=
                (my_morton_nr >= 0)
                    ? my_box_morton_bin_counts.nonchild_srcntgts
                    : 0;
        %endif
        %for mnr in range(2**dimensions):
            <% bin_nmr = padded_bin(mnr, dimensions) %>
            tgt_particle_idx +=
                (my_morton_nr > ${mnr})
                    ? my_box_morton_bin_counts.pcnt${bin_nmr}
                    : 0;
        %endfor

        dbg_assert(tgt_particle_idx < n);
        dbg_printf(("   moving %ld -> %d "
            "(ibox %d, my_box_start %d, my_count %d)\n",
            i, tgt_particle_idx,
            ibox, my_box_start, my_count));

        new_user_srcntgt_ids[tgt_particle_idx] = user_srcntgt_ids[i];

        // }}}

        // {{{ compute this srcntgt's new box id

        box_id_t new_box_id = split_box_ids[i] - ${2**dimensions} + my_morton_nr;

        %if srcntgts_have_extent:
            if (my_morton_nr == -1)
                new_box_id = ibox;
        %endif

        dbg_printf(("   new_box_id: %d\n", new_box_id));
        dbg_assert(new_box_id >= 0);

        new_srcntgt_box_ids[tgt_particle_idx] = new_box_id;

        // }}}

        // {{{ set up child box data structure

        %for mnr in range(2**dimensions):
          /* Am I the last particle in my Morton bin? */
            %if mnr > 0:
                else
            %endif
            if (${mnr} == my_morton_nr
                && my_box_morton_bin_counts.pcnt${padded_bin(mnr, dimensions)}
                    == my_count)
            {
                dbg_printf(("   ## splitting\n"));

                particle_id_t new_box_start = my_box_start
                %if srcntgts_have_extent:
                    + my_box_morton_bin_counts.nonchild_srcntgts
                %endif
                %for sub_mnr in range(mnr):
                    + my_box_morton_bin_counts.pcnt${padded_bin(sub_mnr, dimensions)}
                %endfor
                    ;

                dbg_printf(("   new_box_start: %d\n", new_box_start));

                box_start_flags[new_box_start] = 1;
                box_srcntgt_starts[new_box_id] = new_box_start;
                box_parent_ids[new_box_id] = ibox;
                box_morton_nrs[new_box_id] = my_morton_nr;

                particle_id_t new_count =
                    my_box_morton_bin_counts.pcnt${padded_bin(mnr, dimensions)};
                box_srcntgt_counts_cumul[new_box_id] = new_count;
                box_levels[new_box_id] = level;

                refine_weight_t new_weight =
                    my_box_morton_bin_counts.pwt${padded_bin(mnr, dimensions)};

                // This drives the level loop.
                if (new_weight > max_leaf_refine_weight)
                {
                    *have_oversize_split_box = 1;
                }

                dbg_printf(("   box pcount: %d\n",
                    box_srcntgt_counts_cumul[new_box_id]));
            }
        %endfor

        // }}}
    }
    else
    {
        // Not splitting? Copy over existing particle info.
        new_user_srcntgt_ids[i] = user_srcntgt_ids[i];
        new_srcntgt_box_ids[i] = ibox;
    }
""", strict_undefined=True)

# }}}

# END KERNELS IN THE LEVEL LOOP

# {{{ nonchild srcntgt count extraction

EXTRACT_NONCHILD_SRCNTGT_COUNT_TPL = ElementwiseTemplate(
    arguments="""//CL//
        /* input */
        morton_counts_t *box_morton_bin_counts,
        particle_id_t *box_srcntgt_counts_cumul,
        box_id_t highest_possibly_split_box_nr,

        /* output */
        particle_id_t *box_srcntgt_counts_nonchild,
        """,
    operation=r"""//CL//
        if (i >= highest_possibly_split_box_nr)
        {
            // box_morton_bin_counts gets written in morton scan output.
            // Therefore, newly created boxes in the last level don't
            // have it initialized.

            box_srcntgt_counts_nonchild[i] = 0;
        }
        else if (box_srcntgt_counts_cumul[i] == 0)
        {
            // If boxes are empty, box_morton_bin_counts never gets initialized.
            box_srcntgt_counts_nonchild[i] = 0;
        }
        else
            box_srcntgt_counts_nonchild[i] =
                box_morton_bin_counts[i].nonchild_srcntgts;
        """,
    name="extract_nonchild_srcntgt_count")

# }}}

# {{{ source and target index finding

SOURCE_AND_TARGET_INDEX_FINDER = ElementwiseTemplate(
    arguments="""//CL:mako//
        /* input */
        particle_id_t *user_srcntgt_ids,
        particle_id_t nsources,
        box_id_t *srcntgt_box_ids,
        box_id_t *box_parent_ids,

        particle_id_t *box_srcntgt_starts,
        particle_id_t *box_srcntgt_counts_cumul,
        particle_id_t *source_numbers,

        %if srcntgts_have_extent:
            particle_id_t *box_srcntgt_counts_nonchild,
        %endif

        /* output */
        particle_id_t *user_source_ids,
        particle_id_t *srcntgt_target_ids,
        particle_id_t *sorted_target_ids,

        particle_id_t *box_source_starts,
        particle_id_t *box_source_counts_cumul,
        particle_id_t *box_target_starts,
        particle_id_t *box_target_counts_cumul,

        %if srcntgts_have_extent:
            particle_id_t *box_source_counts_nonchild,
            particle_id_t *box_target_counts_nonchild,
        %endif
        """,
    operation=r"""//CL:mako//
        ## Splitting sources and targets makes no sense when they're the same.
        <% assert not sources_are_targets %>

        particle_id_t sorted_srcntgt_id = i;
        particle_id_t source_nr = source_numbers[i];
        particle_id_t target_nr = i - source_nr;

        box_id_t box_id = srcntgt_box_ids[sorted_srcntgt_id];

        particle_id_t box_start = box_srcntgt_starts[box_id];
        particle_id_t box_count = box_srcntgt_counts_cumul[box_id];

        particle_id_t user_srcntgt_id
            = user_srcntgt_ids[sorted_srcntgt_id];

        bool is_source = user_srcntgt_id < nsources;

        // {{{ write start and end of box in terms of sources and targets

        // first particle for this or the parents' boxes? update starts
        {
            particle_id_t walk_box_start = box_start;
            box_id_t walk_box_id = box_id;

            while (sorted_srcntgt_id == walk_box_start)
            {
                box_source_starts[walk_box_id] = source_nr;
                box_target_starts[walk_box_id] = target_nr;

                box_id_t new_box_id = box_parent_ids[walk_box_id];
                if (new_box_id == walk_box_id)
                {
                    // don't loop at root
                    dbg_assert(walk_box_id == 0);
                    break;
                }

                walk_box_id = new_box_id;
                walk_box_start = box_srcntgt_starts[walk_box_id];
            }
        }

        %if srcntgts_have_extent:
            // last non-child particle?

            // (Can't be "first child particle", because then the box might
            // not have any child particles!)

            particle_id_t box_nonchild_count = box_srcntgt_counts_nonchild[box_id];

            if (sorted_srcntgt_id + 1 == box_start + box_nonchild_count)
            {
                particle_id_t box_start_source_nr = source_numbers[box_start];
                particle_id_t box_start_target_nr = box_start - box_start_source_nr;

                box_source_counts_nonchild[box_id] =
                    source_nr + (particle_id_t) is_source
                    - box_start_source_nr;

                box_target_counts_nonchild[box_id] =
                    target_nr + 1 - (particle_id_t) is_source
                    - box_start_target_nr;
            }
        %elif srcntgts_have_extent:
            box_source_counts_nonchild[box_id] = 0;
            box_target_counts_nonchild[box_id] = 0;
        %endif

        // {{{ last particle for this or the parents' boxes? update counts

        {
            particle_id_t walk_box_start = box_start;
            particle_id_t walk_box_count = box_count;
            box_id_t walk_box_id = box_id;

            while (sorted_srcntgt_id + 1 == walk_box_start + walk_box_count)
            {
                particle_id_t box_start_source_nr =
                    source_numbers[walk_box_start];
                particle_id_t box_start_target_nr =
                    walk_box_start - box_start_source_nr;

                box_source_counts_cumul[walk_box_id] =
                    source_nr + (particle_id_t) is_source
                    - box_start_source_nr;

                box_target_counts_cumul[walk_box_id] =
                    target_nr + 1 - (particle_id_t) is_source
                    - box_start_target_nr;

                box_id_t new_box_id = box_parent_ids[walk_box_id];
                if (new_box_id == walk_box_id)
                {
                    // don't loop at root
                    dbg_assert(walk_box_id == 0);
                    break;
                }

                walk_box_id = new_box_id;

                walk_box_start = box_srcntgt_starts[walk_box_id];
                walk_box_count = box_srcntgt_counts_cumul[walk_box_id];
            }
        }

        // }}}

        // }}}

        if (is_source)
        {
            user_source_ids[source_nr] = user_srcntgt_id;
        }
        else
        {
            particle_id_t user_target_id = user_srcntgt_id - nsources;

            srcntgt_target_ids[target_nr] = user_srcntgt_id;
            sorted_target_ids[user_target_id] = target_nr;
        }

        """,
    name="find_source_and_target_indices")

# }}}

# {{{ source/target permuter

SRCNTGT_PERMUTER_TPL = ElementwiseTemplate(
    arguments="""//CL:mako//
        particle_id_t *from_ids
        %for ax in axis_names:
            , coord_t *${ax}
        %endfor
        %for ax in axis_names:
            , coord_t *sorted_${ax}
        %endfor
        """,
    operation=r"""//CL:mako//
        particle_id_t from_idx = from_ids[i];
        %for ax in axis_names:
            sorted_${ax}[i] = ${ax}[from_idx];
        %endfor
        """,
    name="permute_srcntgt")

# }}}


# {{{ box info kernel

BOX_INFO_KERNEL_TPL = ElementwiseTemplate(
    arguments="""//CL:mako//
        /* input */
        box_id_t *box_parent_ids,
        morton_nr_t *box_morton_nrs,
        bbox_t bbox,
        box_id_t aligned_nboxes,
        particle_id_t *box_srcntgt_counts_cumul,
        particle_id_t *box_source_counts_cumul,
        particle_id_t *box_target_counts_cumul,
        int *box_has_children,
        box_level_t *box_levels,
        box_level_t nlevels,

        /* output if not srcntgts_have_extent, input+output otherwise */
        particle_id_t *box_source_counts_nonchild,
        particle_id_t *box_target_counts_nonchild,

        /* output */
        box_id_t *box_child_ids, /* [2**dimensions, aligned_nboxes] */
        coord_t *box_centers, /* [dimensions, aligned_nboxes] */
        box_flags_t *box_flags, /* [nboxes] */
        """,
    operation=r"""//CL:mako//
        const coord_t one_half = ((coord_t) 1) / 2;

        box_id_t box_id = i;

        /* Note that srcntgt_counts is a cumulative count over all children,
         * up to the point below where it is set to zero for non-leaves.
         *
         * box_srcntgt_counts_cumul is zero (here) exactly for empty leaves
         * because it gets initialized to zero and never gets set to another
         * value. If you check above, most box info is only ever initialized
         * *if* there's a particle in the box, because the sort/build is a
         * repeated scan over *particles* (not boxes). Thus, no particle -> no
         * work done.
         */

        particle_id_t particle_count = box_srcntgt_counts_cumul[box_id];

        /* Up until this point, last-level (=>leaf) boxes may have their
         * non-child counts set to zero. (see
         * EXTRACT_NONCHILD_SRCNTGT_COUNT_TPL)
         * This is fixed below.
         *
         * In addition, upper-level leaves may have only part of their
         * particles declared non-child (i.e. "not fit for downward propagation")
         * Here, we reclassify all particles in leaves as non-child.
         */

        %if srcntgts_have_extent:
            const particle_id_t nonchild_source_count =
                box_source_counts_nonchild[box_id];
            const particle_id_t nonchild_target_count =
                box_target_counts_nonchild[box_id];
        %else:
            const particle_id_t nonchild_source_count = 0;
            const particle_id_t nonchild_target_count = 0;
        %endif

        const particle_id_t nonchild_srcntgt_count
            = nonchild_source_count + nonchild_target_count;

        box_flags_t my_box_flags = 0;

        dbg_assert(particle_count >= nonchild_srcntgt_count);

        if (particle_count == 0)
        {
            // Empty leaf: Lots of stuff uninitialized, prevent
            // damage by quitting now.

            // Also, those should have gotten pruned by this point,
            // unless skip_prune is True.

            box_flags[box_id] = 0; // no children, no sources, no targets, bye.

            PYOPENCL_ELWISE_CONTINUE;
        }
        else if (box_has_children[box_id])
        {
            // This box has children, it is not a leaf.

            my_box_flags |= BOX_HAS_CHILDREN;

            %if sources_are_targets:
                if (particle_count - nonchild_srcntgt_count)
                    my_box_flags |= BOX_HAS_CHILD_SOURCES | BOX_HAS_CHILD_TARGETS;
            %else:
                particle_id_t source_count = box_source_counts_cumul[box_id];
                particle_id_t target_count = box_target_counts_cumul[box_id];

                dbg_assert(source_count >= nonchild_source_count);
                dbg_assert(target_count >= nonchild_target_count);

                if (source_count - nonchild_source_count)
                    my_box_flags |= BOX_HAS_CHILD_SOURCES;
                if (target_count - nonchild_target_count)
                    my_box_flags |= BOX_HAS_CHILD_TARGETS;
            %endif

            if (nonchild_source_count)
                my_box_flags |= BOX_HAS_OWN_SOURCES;
            if (nonchild_target_count)
                my_box_flags |= BOX_HAS_OWN_TARGETS;
        }
        else
        {
            // This box is a leaf, i.e. it has no children.

            %if sources_are_targets:
                if (particle_count)
                    my_box_flags |= BOX_HAS_OWN_SOURCES | BOX_HAS_OWN_TARGETS;

                box_source_counts_nonchild[box_id] = particle_count;
                dbg_assert(box_source_counts_nonchild == box_target_counts_nonchild);
            %else:
                particle_id_t my_source_count = box_source_counts_cumul[box_id];
                particle_id_t my_target_count = particle_count - my_source_count;

                if (my_source_count)
                    my_box_flags |= BOX_HAS_OWN_SOURCES;
                if (my_target_count)
                    my_box_flags |= BOX_HAS_OWN_TARGETS;

                box_source_counts_nonchild[box_id] = my_source_count;
                box_target_counts_nonchild[box_id] = my_target_count;
            %endif
        }

        box_flags[box_id] = my_box_flags;

        box_id_t parent_id = box_parent_ids[box_id];
        morton_nr_t morton_nr = box_morton_nrs[box_id];
        box_child_ids[parent_id + aligned_nboxes*morton_nr] = box_id;

        /* walk up to root to find center */
        %for idim in range(dimensions):
            coord_t center_${idim} = 0;
        %endfor

        box_id_t walk_parent_id = parent_id;
        box_id_t current_box_id = box_id;
        morton_nr_t walk_morton_nr = morton_nr;
        while (walk_parent_id != current_box_id)
        {
            %for idim in range(dimensions):
                {
                    bool has_bit = (walk_morton_nr & ${2**(dimensions-1-idim)});
                    center_${idim} = one_half*(
                        center_${idim}
                        - one_half
                        + has_bit);
                }
            %endfor

            current_box_id = walk_parent_id;
            walk_parent_id = box_parent_ids[walk_parent_id];
            walk_morton_nr = box_morton_nrs[current_box_id];
        }

        coord_t extent = bbox.max_x - bbox.min_x;
        %for idim in range(dimensions):
        {
            box_centers[box_id + aligned_nboxes*${idim}] =
                bbox.min_${AXIS_NAMES[idim]} + extent*(one_half+center_${idim});
        }
        %endfor
    """)

# }}}


# {{{ kernel creation top-level

def get_tree_build_kernel_info(context, dimensions, coord_dtype,
        particle_id_dtype, box_id_dtype,
        sources_are_targets, srcntgts_have_extent,
        stick_out_factor, morton_nr_dtype, box_level_dtype,
        adaptive):

    logger.info("start building tree build kernels")

    # {{{ preparation

    if np.iinfo(box_id_dtype).min == 0:
        from warnings import warn
        warn("Careful with unsigned types for box_id_dtype. Some CL implementations "
                "(notably Intel 2012) mis-implemnet unsigned operations, leading to "
                "incorrect results.", stacklevel=4)

    from pyopencl.tools import dtype_to_c_struct, dtype_to_ctype
    coord_vec_dtype = cl.array.vec.types[coord_dtype, dimensions]

    particle_id_dtype = np.dtype(particle_id_dtype)
    box_id_dtype = np.dtype(box_id_dtype)

    dev = context.devices[0]
    morton_bin_count_dtype, _ = make_morton_bin_count_type(
            dev, dimensions, particle_id_dtype,
            srcntgts_have_extent)

    from boxtree.bounding_box import make_bounding_box_dtype
    bbox_dtype, bbox_type_decl = make_bounding_box_dtype(
            dev, dimensions, coord_dtype)

    from boxtree.tools import AXIS_NAMES
    axis_names = AXIS_NAMES[:dimensions]

    from boxtree.tools import padded_bin
    from boxtree.tree import box_flags_enum
    codegen_args = dict(
            dimensions=dimensions,
            axis_names=axis_names,
            padded_bin=padded_bin,
            coord_dtype=coord_dtype,
            coord_vec_dtype=coord_vec_dtype,
            bbox_dtype=bbox_dtype,
            refine_weight_dtype=refine_weight_dtype,
            particle_id_dtype=particle_id_dtype,
            morton_bin_count_dtype=morton_bin_count_dtype,
            morton_nr_dtype=morton_nr_dtype,
            box_id_dtype=box_id_dtype,
            dtype_to_ctype=dtype_to_ctype,
            AXIS_NAMES=AXIS_NAMES,
            box_flags_enum=box_flags_enum,

            adaptive=adaptive,

            sources_are_targets=sources_are_targets,
            srcntgts_have_extent=srcntgts_have_extent,

            stick_out_factor=stick_out_factor,

            enable_assert=False,
            enable_printf=False,
            )

    # }}}

    generic_preamble = str(GENERIC_PREAMBLE_TPL.render(**codegen_args))

    preamble_with_dtype_decls = (
            dtype_to_c_struct(dev, bbox_dtype)
            + dtype_to_c_struct(dev, morton_bin_count_dtype)
            + str(TYPE_DECL_PREAMBLE_TPL.render(**codegen_args))
            + generic_preamble
            )

    # BEGIN KERNELS IN LEVEL LOOP

    # {{{ scan

    scan_preamble = (
            preamble_with_dtype_decls
            + str(SCAN_PREAMBLE_TPL.render(**codegen_args))
            )

    from pyopencl.tools import VectorArg, ScalarArg
    common_arguments = (
            [
                # box-local morton bin counts for each particle at the current level
                # only valid from scan -> split'n'sort

                VectorArg(morton_bin_count_dtype, "morton_bin_counts"),
                # [nsrcntgts]

                # (local) morton nrs for each particle at the current level
                # only valid from scan -> split'n'sort
                VectorArg(morton_nr_dtype, "morton_nrs"),  # [nsrcntgts]

                # segment flags
                # invariant to sorting once set
                # (particles are only reordered within a box)
                VectorArg(np.uint8, "box_start_flags"),  # [nsrcntgts]

                VectorArg(box_id_dtype, "srcntgt_box_ids"),  # [nsrcntgts]
                VectorArg(box_id_dtype, "split_box_ids"),  # [nsrcntgts]

                # per-box morton bin counts
                VectorArg(morton_bin_count_dtype, "box_morton_bin_counts"),
                # [nsrcntgts]

                VectorArg(refine_weight_dtype, "refine_weights"),
                # [nsrcntgts]

                ScalarArg(refine_weight_dtype, "max_leaf_refine_weight"),

                # particle# at which each box starts
                VectorArg(particle_id_dtype, "box_srcntgt_starts"),  # [nboxes]

                # number of particles in each box
                VectorArg(particle_id_dtype, "box_srcntgt_counts_cumul"),  # [nboxes]

                # pointer to parent box
                VectorArg(box_id_dtype, "box_parent_ids"),  # [nboxes]

                # morton nr identifier {quadr,oct}ant of parent in which this
                # box was created
                VectorArg(morton_nr_dtype, "box_morton_nrs"),  # [nboxes]

                # number of boxes total
                VectorArg(box_id_dtype, "nboxes"),  # [1]

                ScalarArg(np.int32, "level"),
                ScalarArg(bbox_dtype, "bbox"),

                VectorArg(particle_id_dtype, "user_srcntgt_ids"),  # [nsrcntgts]
                ]

            # particle coordinates
            + [VectorArg(coord_dtype, ax) for ax in axis_names]

            + ([VectorArg(coord_dtype, "srcntgt_radii")]
                if srcntgts_have_extent else [])
            )

    from pyopencl.scan import GenericScanKernel
    morton_count_scan = GenericScanKernel(
            context, morton_bin_count_dtype,
            arguments=common_arguments,
            input_expr=(
                "scan_t_from_particle(%s)"
                % ", ".join([
                    "i", "level", "&bbox", "morton_nrs",
                    "user_srcntgt_ids",
                    "refine_weights",
                    ]
                    + ["%s" % ax for ax in axis_names]
                    + (["srcntgt_radii"] if srcntgts_have_extent else []))),
            scan_expr="scan_t_add(a, b, across_seg_boundary)",
            neutral="scan_t_neutral()",
            is_segment_start_expr="box_start_flags[i]",
            output_statement=SCAN_OUTPUT_STMT_TPL.render(**codegen_args),
            preamble=scan_preamble)

    # }}}

    # {{{ split_box_id scan

    from pyopencl.scan import GenericScanKernel
    split_box_id_scan = SPLIT_BOX_ID_SCAN_TPL.build(
            context,
            type_aliases=(
                ("scan_t", box_id_dtype),
                ("index_t", particle_id_dtype),
                ("particle_id_t", particle_id_dtype),
                ("box_id_t", box_id_dtype),
                ("morton_counts_t", morton_bin_count_dtype),
                ("box_level_t", box_level_dtype),
                ("refine_weight_t", refine_weight_dtype),
                ),
            var_values=(
                ("dimensions", dimensions),
                ("srcntgts_have_extent", srcntgts_have_extent),
                ("adaptive", adaptive),
                ("padded_bin", padded_bin),
                ),
            more_preamble=generic_preamble)

    # }}}

    # {{{ split-and-sort

    # Work around a bug in Mako < 0.7.3
    s_and_s_codegen_args = codegen_args.copy()
    s_and_s_codegen_args.update(
            dim=None,
            boundary_morton_nr=None)

    split_and_sort_preamble = \
            SPLIT_AND_SORT_PREAMBLE_TPL.render(**s_and_s_codegen_args)

    split_and_sort_kernel_source = SPLIT_AND_SORT_KERNEL_TPL.render(**codegen_args)

    from pyopencl.elementwise import ElementwiseKernel
    split_and_sort_kernel = ElementwiseKernel(
            context,
            common_arguments
            + [
                VectorArg(particle_id_dtype, "new_user_srcntgt_ids",
                    with_offset=True),
                VectorArg(np.int32, "have_oversize_split_box", with_offset=True),
                VectorArg(box_id_dtype, "new_srcntgt_box_ids", with_offset=True),
                VectorArg(box_level_dtype, "box_levels", with_offset=True),
                VectorArg(np.int32, "box_has_children", with_offset=True),
                ],
            str(split_and_sort_kernel_source), name="split_and_sort",
            preamble=(
                preamble_with_dtype_decls
                + str(split_and_sort_preamble))
            )

    # }}}

    # END KERNELS IN LEVEL LOOP

    if srcntgts_have_extent:
        extract_nonchild_srcntgt_count_kernel = \
                EXTRACT_NONCHILD_SRCNTGT_COUNT_TPL.build(
                        context,
                        type_aliases=(
                            ("particle_id_t", particle_id_dtype),
                            ("box_id_t", box_id_dtype),
                            ("morton_counts_t", morton_bin_count_dtype),
                            ),
                        var_values=(),
                        more_preamble=generic_preamble)

    else:
        extract_nonchild_srcntgt_count_kernel = None

    # {{{ find-prune-indices

    # FIXME: Turn me into a scan template

    from pyopencl.tools import VectorArg
    find_prune_indices_kernel = GenericScanKernel(
            context, box_id_dtype,
            arguments=[
                # input
                VectorArg(particle_id_dtype, "box_srcntgt_counts_cumul"),
                # output
                VectorArg(box_id_dtype, "to_box_id"),
                VectorArg(box_id_dtype, "from_box_id"),
                VectorArg(box_id_dtype, "nboxes_post_prune"),
                ],
            input_expr="box_srcntgt_counts_cumul[i] == 0 ? 1 : 0",
            preamble=box_flags_enum.get_c_defines(),
            scan_expr="a+b", neutral="0",
            output_statement="""
                to_box_id[i] = i-prev_item;
                if (box_srcntgt_counts_cumul[i])
                    from_box_id[i-prev_item] = i;
                if (i+1 == N) *nboxes_post_prune = N-item;
                """)

    # }}}

    # {{{ particle permuter

    # used if there is only one source/target array
    srcntgt_permuter = SRCNTGT_PERMUTER_TPL.build(
            context,
            type_aliases=(
                ("particle_id_t", particle_id_dtype),
                ("box_id_t", box_id_dtype),
                ("coord_t", coord_dtype),
                ),
            var_values=(
                ("axis_names", axis_names),
                ),
            more_preamble=generic_preamble)

    # }}}

    # {{{ source-and-target splitter

    # These kernels are only needed if there are separate sources and
    # targets.  But they're only built on-demand anyway, so that's not
    # really a loss.

    # FIXME: make me a scan template
    from pyopencl.scan import GenericScanKernel
    source_counter = GenericScanKernel(
            context, box_id_dtype,
            arguments=[
                # input
                VectorArg(particle_id_dtype, "user_srcntgt_ids"),
                ScalarArg(particle_id_dtype, "nsources"),
                # output
                VectorArg(particle_id_dtype, "source_numbers"),
                ],
            input_expr="(user_srcntgt_ids[i] < nsources) ? 1 : 0",
            scan_expr="a+b", neutral="0",
            output_statement="source_numbers[i] = prev_item;",
            name_prefix="source_counter")

    if not sources_are_targets:
        source_and_target_index_finder = SOURCE_AND_TARGET_INDEX_FINDER.build(
                context,
                type_aliases=(
                    ("particle_id_t", particle_id_dtype),
                    ("box_id_t", box_id_dtype),
                    ),
                var_values=(
                    ("srcntgts_have_extent", srcntgts_have_extent),
                    ("sources_are_targets", sources_are_targets),
                    ),
                more_preamble=generic_preamble)
    else:
        source_and_target_index_finder = None

    # }}}

    # {{{ box-info

    type_aliases = (
            ("box_id_t", box_id_dtype),
            ("particle_id_t", particle_id_dtype),
            ("bbox_t", bbox_dtype),
            ("coord_t", coord_dtype),
            ("morton_nr_t", morton_nr_dtype),
            ("coord_vec_t", coord_vec_dtype),
            ("box_flags_t", box_flags_enum.dtype),
            ("box_level_t", box_level_dtype),
            )
    codegen_args_tuples = tuple(six.iteritems(codegen_args))
    box_info_kernel = BOX_INFO_KERNEL_TPL.build(
            context,
            type_aliases,
            var_values=codegen_args_tuples,
            more_preamble=box_flags_enum.get_c_defines() + generic_preamble,
            )

    # }}}

    logger.info("tree build kernels built")

    return _KernelInfo(
            particle_id_dtype=particle_id_dtype,
            box_id_dtype=box_id_dtype,
            morton_bin_count_dtype=morton_bin_count_dtype,

            morton_count_scan=morton_count_scan,
            split_box_id_scan=split_box_id_scan,
            split_and_sort_kernel=split_and_sort_kernel,

            extract_nonchild_srcntgt_count_kernel=(
                extract_nonchild_srcntgt_count_kernel),
            find_prune_indices_kernel=find_prune_indices_kernel,
            srcntgt_permuter=srcntgt_permuter,
            source_counter=source_counter,
            source_and_target_index_finder=source_and_target_index_finder,
            box_info_kernel=box_info_kernel,
            )

# }}}


# {{{ point source linking kernels

# scan over (non-point) source ids in tree order
POINT_SOURCE_LINKING_SOURCE_SCAN_TPL = ScanTemplate(
    arguments=r"""//CL:mako//
        /* input */
        particle_id_t *point_source_starts,
        particle_id_t *user_source_ids,

        /* output */
        particle_id_t *tree_order_point_source_starts,
        particle_id_t *tree_order_point_source_counts,
        particle_id_t *npoint_sources
        """,
    input_expr="""
        point_source_starts[user_source_ids[i]+1]
        - point_source_starts[user_source_ids[i]]
        """,
    scan_expr="a + b",
    neutral="0",
    output_statement="""//CL//
        tree_order_point_source_starts[i] = prev_item;
        tree_order_point_source_counts[i] = item - prev_item;

        // Am I the last particle overall? If so, write point source count
        if (i+1 == N)
            *npoint_sources = item;
        """)

POINT_SOURCE_LINKING_USER_POINT_SOURCE_ID_SCAN_TPL = ScanTemplate(
    arguments=r"""//CL:mako//
        char *source_boundaries,
        particle_id_t *user_point_source_ids
        """,
    input_expr="user_point_source_ids[i]",
    scan_expr="across_seg_boundary ? b : a + b",
    neutral="0",
    is_segment_start_expr="source_boundaries[i]",
    output_statement="user_point_source_ids[i] = item;")

POINT_SOURCE_LINKING_BOX_POINT_SOURCES = ElementwiseTemplate(
    arguments="""//CL//
        particle_id_t *box_point_source_starts,
        particle_id_t *box_point_source_counts_nonchild,
        particle_id_t *box_point_source_counts_cumul,

        particle_id_t *box_source_starts,
        particle_id_t *box_source_counts_nonchild,
        particle_id_t *box_source_counts_cumul,

        particle_id_t *tree_order_point_source_starts,
        particle_id_t *tree_order_point_source_counts,
        """,
    operation=r"""//CL:mako//
        box_id_t ibox = i;
        particle_id_t s_start = box_source_starts[ibox];
        particle_id_t ps_start = tree_order_point_source_starts[s_start];

        box_point_source_starts[ibox] = ps_start;

        %for count_type in ["nonchild", "cumul"]:
            {
                particle_id_t s_count = box_source_counts_${count_type}[ibox];
                if (s_count == 0)
                {
                    box_point_source_counts_${count_type}[ibox] = 0;
                }
                else
                {
                    particle_id_t last_s_in_box = s_start+s_count-1;
                    particle_id_t beyond_last_ps_in_box =
                        tree_order_point_source_starts[last_s_in_box]
                        + tree_order_point_source_counts[last_s_in_box];
                    box_point_source_counts_${count_type}[ibox] = \
                            beyond_last_ps_in_box - ps_start;
                }
            }
        %endfor
        """,
    name="box_point_sources")

# }}}


# {{{ target filtering

TREE_ORDER_TARGET_FILTER_SCAN_TPL = ScanTemplate(
    arguments=r"""//CL:mako//
        /* input */
        unsigned char *tree_order_flags,

        /* output */
        particle_id_t *filtered_from_unfiltered_target_index,
        particle_id_t *unfiltered_from_filtered_target_index,
        particle_id_t *nfiltered_targets
        """,
    input_expr="tree_order_flags[i] ? 1 : 0",
    scan_expr="a + b",
    neutral="0",
    output_statement="""//CL//
        filtered_from_unfiltered_target_index[i] = prev_item;
        if (item != prev_item)
            unfiltered_from_filtered_target_index[prev_item] = i;

        // Am I the last particle overall? If so, write count
        if (i+1 == N)
            *nfiltered_targets = item;
        """)

TREE_ORDER_TARGET_FILTER_INDEX_TPL = ElementwiseTemplate(
    arguments="""//CL//
        /* input */
        particle_id_t *box_target_starts,
        particle_id_t *box_target_counts_nonchild,
        particle_id_t *filtered_from_unfiltered_target_index,
        particle_id_t ntargets,
        particle_id_t nfiltered_targets,

        /* output */
        particle_id_t *box_target_starts_filtered,
        particle_id_t *box_target_counts_nonchild_filtered,
        """,
    operation=r"""//CL//
        particle_id_t unfiltered_start = box_target_starts[i];
        particle_id_t unfiltered_count = box_target_counts_nonchild[i];

        particle_id_t filtered_start =
            filtered_from_unfiltered_target_index[unfiltered_start];
        box_target_starts_filtered[i] = filtered_start;

        if (unfiltered_count > 0)
        {
            particle_id_t unfiltered_post_last =
                unfiltered_start + unfiltered_count;

            particle_id_t filtered_post_last;
            if (unfiltered_post_last < ntargets)
            {
                filtered_post_last =
                    filtered_from_unfiltered_target_index[unfiltered_post_last];
            }
            else
            {
                // The above access would be out of bounds in this case.
                filtered_post_last = nfiltered_targets;
            }

            box_target_counts_nonchild_filtered[i] =
                filtered_post_last - filtered_start;
        }
        else
            box_target_counts_nonchild_filtered[i] = 0;
        """,
    name="tree_order_target_filter_index_finder")

# }}}

# vim: foldmethod=marker:filetype=pyopencl
