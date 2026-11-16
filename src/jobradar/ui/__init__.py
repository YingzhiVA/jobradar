"""A local browser interface: ``python -m jobradar.ui``.

A second way to work with an instance that is already installed, beside the
terminal and an editor, not a replacement for either. It serves one page on
``127.0.0.1``: a map of the workflow (setup, search, apply) with where each
stage stands, and views to do each stage from.

It adds no second implementation of anything. A form writes the file the docs
have you edit by hand, changing only the lines it must (``yamledit.py``), and
every action that searches, drafts or records an outcome runs the documented
command as a subprocess (``jobs.py``) and shows it. So the page teaches the
terminal workflow as a side effect, and the two can be mixed freely.

    setup.py     read and write the profile and config files
    yamledit.py  change values in a YAML file, keeping its comments
    views.py     read the report and the application tracker
    jobs.py      run a jobradar command, keep its output
    server.py    the HTTP layer over them
    static/      the page itself: plain HTML, CSS and JavaScript, no build step
"""
