"""Application-tailoring pipeline: turn a list of job-posting URLs into
git-tracked, ready-to-review application folders (tailored CV, cover letter
when required, and notes), reusing the daily jobradar report's analysis when
the posting has already been scored.

Entry point: python -m jobradar.apply
"""
