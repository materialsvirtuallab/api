from fastapi import HTTPException
from mp_api.core.resource import Resource
from mp_api.materials.models.core import Structure
from mp_api.materials.models.doc import MaterialsCoreDoc


from mp_api.core.query_operator import (
    PaginationQuery,
    SparseFieldsQuery,
    VersionQuery,
    SortQuery,
)
from mp_api.materials.query_operators import (
    FormulaQuery,
    DeprecationQuery,
    MinMaxQuery,
    SymmetryQuery,
    MultiTaskIDQuery,
)

from pymatgen.analysis.structure_matcher import StructureMatcher, ElementComparator
from pymatgen.core import Structure as PS
from pymatgen.core import Composition
from pymongo import MongoClient  # type: ignore
from itertools import permutations
from fastapi import Query, Body


def materials_resource(materials_store):
    def custom_version_prep(self):
        model_name = self.model.__name__

        async def get_versions():
            f"""
            Obtains the database versions for the data in {model_name}

            Returns:
                A list of database versions one can use to query on
            """

            try:
                conn = MongoClient(self.store.host, self.store.port)
                db = conn[self.store.database]
                if self.core.username != "":
                    db.authenticate(self.username, self.password)

            except AttributeError:
                conn = MongoClient(self.store.uri)
                db = conn[self.store.database]

            col_names = db.list_collection_names()

            d = [
                name.replace("_", ".")[15:]
                for name in col_names
                if "materials" in name
                if name != "materials.core"
            ]

            response = {"data": d}

            return response

        self.router.get(
            "/versions/",
            response_model_exclude_unset=True,
            response_description=f"Get versions of {model_name}",
            tags=self.tags,
        )(get_versions)

    def custom_findstructure_prep(self):
        model_name = self.model.__name__

        async def find_structure(
            structure: Structure = Body(
                ...,
                title="Pymatgen structure object to query with",
            ),
            ltol: float = Query(
                0.2,
                title="Fractional length tolerance. Default is 0.2.",
            ),
            stol: float = Query(
                0.3,
                title="Site tolerance. Defined as the fraction of the average free \
                    length per atom := ( V / Nsites ) ** (1/3). Default is 0.3.",
            ),
            angle_tol: float = Query(
                5,
                title="Angle tolerance in degrees. Default is 5 degrees.",
            ),
            limit: int = Query(
                1,
                title="Maximum number of matches to show. Defaults to 1, only showing the best match.",
            ),
        ):
            """
            Obtains material structures that match a given input structure within some tolerance.

            Returns:
                A list of Material IDs for materials with matched structures alongside the associated RMS values
            """

            try:
                s = PS.from_dict(structure.dict())
            except Exception:
                raise HTTPException(
                    status_code=404,
                    detail="Body cannot be converted to a pymatgen structure object.",
                )

            m = StructureMatcher(
                ltol=ltol,
                stol=stol,
                angle_tol=angle_tol,
                primitive_cell=True,
                scale=True,
                attempt_supercell=False,
                comparator=ElementComparator(),
            )

            crit = {"composition_reduced": dict(s.composition.to_reduced_dict)}

            self.store.connect()

            matches = []

            for r in self.store.query(
                criteria=crit, properties=["structure", "task_id"]
            ):

                s2 = PS.from_dict(r["structure"])
                matched = m.fit(s, s2)

                if matched:
                    rms = m.get_rms_dist(s, s2)

                    matches.append(
                        {
                            "task_id": r["task_id"],
                            "normalized_rms_displacement": rms[0],
                            "max_distance_paired_sites": rms[1],
                        }
                    )

            response = {
                "data": sorted(
                    matches[:limit],
                    key=lambda x: (
                        x["normalized_rms_displacement"],
                        x["max_distance_paired_sites"],
                    ),
                )
            }

            return response

        self.router.post(
            "/find_structure/",
            response_model_exclude_unset=True,
            response_description=f"Get matching structures using data from {model_name}",
            tags=self.tags,
        )(find_structure)

    def custom_autocomplete_prep(self):
        async def formula_autocomplete(
            text: str = Query(
                ...,
                description="Text to run against formula autocomplete",
            ),
            limit: int = Query(
                10,
                description="Maximum number of matches to show. Defaults to 10",
            ),
        ):

            comp = Composition(text)

            ind_str = []

            if len(comp) == 1:
                d = comp.get_integer_formula_and_factor()

                s = d[0] + str(int(d[1])) if d[1] != 1 else d[0]
                print(s)

                ind_str.append(s)
            else:

                comp_red = comp.reduced_composition.items()

                for (i, j) in comp_red:

                    if j != 1:
                        ind_str.append(i.name + str(int(j)))
                    else:
                        ind_str.append(i.name)

            final_terms = ["".join(entry) for entry in permutations(ind_str)]

            print(final_terms)

            pipeline = [
                {
                    "$search": {
                        "index": "formula_autocomplete",
                        "autocomplete": {
                            "path": "formula_pretty",
                            "query": final_terms,
                            "tokenOrder": "any",
                        },
                    }
                },
                {
                    "$group": {
                        "_id": "$formula_pretty",
                    }
                },
                {"$project": {"score": {"$strLenCP": "$_id"}}},
                {"$sort": {"score": 1}},
                {"$limit": limit},
            ]

            self.store.connect()

            data = list(self.store._collection.aggregate(pipeline, allowDiskUse=True))

            response = {"data": data}

            return response

        self.router.get(
            "/formula_autocomplete/",
            response_model_exclude_unset=True,
            response_description="Get autocomplete results for a formula",
            tags=self.tags,
        )(formula_autocomplete)

    resource = Resource(
        materials_store,
        MaterialsCoreDoc,
        query_operators=[
            VersionQuery(),
            FormulaQuery(),
            MultiTaskIDQuery(),
            SymmetryQuery(),
            DeprecationQuery(),
            MinMaxQuery(),
            SortQuery(),
            PaginationQuery(),
            SparseFieldsQuery(
                MaterialsCoreDoc,
                default_fields=["task_id", "formula_pretty", "last_updated"],
            ),
        ],
        tags=["Materials"],
        custom_endpoint_funcs=[
            custom_version_prep,
            custom_findstructure_prep,
            custom_autocomplete_prep,
        ],
    )

    return resource
