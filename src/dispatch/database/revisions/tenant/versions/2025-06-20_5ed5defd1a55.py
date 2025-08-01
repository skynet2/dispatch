"""Add stable_at column to case table
Revision ID: 5ed5defd1a55
Revises: 7fc3888c7b9a
Create Date: 2025-06-20 11:59:13.546032

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "5ed5defd1a55"
down_revision = "7fc3888c7b9a"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("case", sa.Column("stable_at", sa.DateTime(), nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("case", "stable_at")
    # ### end Alembic commands ###
