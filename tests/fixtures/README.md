# Test fixtures

The `.kdenlive` files in this directory are **copies of Kdenlive's own test
projects**, taken from the upstream repository:

    https://github.com/KDE/kdenlive/tree/master/tests/dataset

They are used as ground truth: any change to the project model in this
repository has to keep these files readable, and the layout they use (where
effects live, how producers are ordered, how the sequence tractor is built)
is what the implementation mirrors.

## Licence

These files are part of Kdenlive and are licensed under the **GPL-3.0**, not
under the MIT licence of the surrounding project. They are unmodified copies
used as test data.

* Kdenlive — https://kdenlive.org
* Copyright (C) the Kdenlive contributors
* Licence text — https://github.com/KDE/kdenlive/blob/master/COPYING

If you refresh these files, keep them unmodified and keep this notice.
