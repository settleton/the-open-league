from backends.tonapi import TonapiAdapter
from backends.contracts_executor import ContractsExecutor
from models.season_config import SeasonConfig
from loguru import logger
import requests
import psycopg2
import time
import base64
from tonsdk.boc import Cell
from tonsdk.utils import Address

"""
Simple tool to sync cNFT SBT collection used for enrollment. 
1. Fetches latest cNFT merkle proof hash
2. Requests full lists of SBTs and checks the proof
3. Inserts list of participants into DB

DB structure:

create table tol.enrollment_{season_name} (
  id serial primary key,
  address varchar,
  sbt varchar,
  added_at timestamp
)

CREATE UNIQUE INDEX enrollment_{season_name}_address_idx ON tol.enrollment_{season_name (address);
"""
API_BASE_URL = "https://stg.globalsociety.cc/v1/csbts/"

class SBTEnrollmentSync:
    def __init__(self, connection, tonapi: TonapiAdapter, executor: ContractsExecutor):
        self.connection = connection
        self.tonapi = tonapi
        self.executor = executor

    def sync(self, config: SeasonConfig):
        logger.info(f"Requesting state for {config.enrollment_sbt}")
        end_block = None
        if int(time.time()) > config.end_time:
            end_block = config.block_before_end_ref
            assert end_block is not None, "Season is closed, one need to specify last block ref"
        code, data = self.tonapi.get_state(config.enrollment_sbt, target_block=end_block)
        # logger.info(f"Got state: {state}")
        [merkle_root] = self.executor.execute(code, data, config.enrollment_sbt, 'get_merkle_root', ['int'])
        merkle_root_hex = f"{int(merkle_root):064x}"
        logger.info(f"Requesting data for {merkle_root_hex}")
        start = 0
        PAGE = 100 # increase to 1000
        total = None
        owners = set()
        while True:
          part = requests.get(f"{API_BASE_URL}{merkle_root_hex}/items?_start={start}&_end={start + PAGE}").json()
          assert total is None or total == part['data']['total']
          total = part['data']['total']
          for item in part['data']['items']:
            data_cell = item['data_cell']
            cell = Cell.one_from_boc(base64.b64decode(data_cell))
            # TODO check hash and extract owner
            owner = Address(item['metadata']['owner']).to_string(1, 1, 1)
            owners.add(owner)
          if len(owners) == total:
            break
          else:
            logger.info(f"Got {len(owners)} items, need {total}")
            start += PAGE
        logger.info(f"Got {len(owners)} owners to update")
        with self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
          for owner in owners:
            cursor.execute(f"""insert into tol.enrollment_{config.safe_season_name()}(address, added_at)
            values (%s, now())
            on conflict do nothing
            """, (owner, ))
        self.connection.commit()
